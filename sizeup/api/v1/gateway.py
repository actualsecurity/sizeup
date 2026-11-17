"""The one way a v1 handler reaches the CAD (#562, docs/mobile-api.md section 1.4: the `CadApi.permit` trap).

A handler is never handed a CadApi (which checks nothing for a request with no person) or the Cad service. It gets a
Gateway, and asks it to act as the signed-in device's person: `acting(principal)` is None for anything that is not a
person's device, and an Acting otherwise. An Acting can only do a few narrow things, all as that person (their email:
the console's role check, their operator name and their activity row apply): look a unit, alarm or incident up,
permit and post a console write, and add a note. Nothing here returns a store, a connection or SQL."""
from __future__ import annotations

import json
import threading
from datetime import datetime

from sizeup.cad.activity import client_tag
from sizeup.cad.store import OPEN, TZ, CadError
from sizeup.cloud.access import Identity

from .idempotency import Idempotency
from .principal import person


class Acting:
    def __init__(self, api, principal):
        self._api, self.principal = api, principal
        self._cad = api.cad
        self.who = Identity(email=principal.email, session=principal.device)     # never who=None

    # -- reads a write checks first -------------------------------------------------------------------------------

    def now(self) -> float:
        return self._cad.store.clock()

    @staticmethod
    def is_open(incident: dict) -> bool:
        return incident["status"] in OPEN

    @staticmethod
    def hms(t: float) -> str:
        return datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S")

    def client_tag(self, via: str, app: str, client_at: float | None = None, clamped: bool = False) -> str:
        """The audit tag of this device's writes (activity.client_tag)."""
        return client_tag(via, self.principal.device, app, client_at, clamped)

    def log(self, message: str) -> None:
        self._cad.log(message)

    def unit(self, code: str) -> dict | None:
        """The unit with this code (any case), as the CAD lists it; None when there is none."""
        return next((u for u in self._cad.store.units() if u["name"].upper() == code.upper()), None)

    def unit_by_id(self, uid: int) -> dict | None:
        return next((u for u in self._cad.store.units() if u["id"] == uid), None)

    def alarm(self, aid: int | None) -> dict | None:
        try:
            return self._cad.alarms.get(aid) if aid else None
        except CadError:
            return None

    def ack_name(self, alarm: dict) -> str:
        """Who acknowledged it, without the e-mail address (the alarms' own helper)."""
        return self._cad.alarms.ack_name(alarm)

    def incident(self, iid: int | None) -> dict | None:
        try:
            return self._cad.store.incident(iid) if iid else None
        except CadError:
            return None

    # -- writes, as this person -----------------------------------------------------------------------------------

    def permit(self, path: str, client: tuple[str, str]):
        """None when the console lets this person POST path; else its 403 (in their activity)."""
        return self._api.permit(path, self.who, None, client=client)

    def post(self, path: str, body: dict, client: tuple[str, str]) -> tuple[int, dict]:
        """The console's write, as this person (CadApi.post: role, operator, validation, activity row).
        (status, the answer as a dict)."""
        status, raw, _ = self._api.post(path, json.dumps(body).encode(), self.who, client)
        try:
            out = json.loads(raw)
        except ValueError:
            out = {}
        return status, out if isinstance(out, dict) else {}

    def note_late(self, iid: int, text: str, client: tuple[str, str]) -> None:
        """A chronology line (a queued action's "queued at ..."), through the console's own note: the role check, the
        operator and an activity row of its own. A line that cannot be added never undoes the write it describes."""
        status, _ = self.post(f"/api/cad/incident/{iid}/note", {"text": text}, client)
        if status != 200:
            self.log(f"api v1: late line refused ({status})")


class Gateway:
    def __init__(self, api):
        self._api = api
        self._idem: Idempotency | None = None
        self._lock = threading.Lock()

    def acting(self, principal) -> Acting | None:
        return Acting(self._api, principal) if person(principal) else None

    def idempotency(self) -> Idempotency:
        """The key store, made on the first write (nothing before: no table, no file)."""
        with self._lock:
            if self._idem is None:
                self._idem = Idempotency.for_cad(self._api.cad)
            return self._idem
