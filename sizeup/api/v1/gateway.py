"""The one way a v1 handler reaches the CAD (#562, docs/mobile-api.md section 1.4: the `CadApi.permit` trap).

A handler is never handed a CadApi (which checks nothing for a request with no person). It gets a Gateway, and asks it to
act as the signed-in device's person: `acting(principal)` is None for anything that is not a person's device, and an
Acting otherwise. An Acting can only post and permit as that person (their email, so the console's role check, their
operator name and their activity row all apply); `cad` is the CAD service, for the reads a write checks first."""
from __future__ import annotations

import json
import threading

from sizeup.cloud.access import Identity

from .idempotency import Idempotency
from .principal import Principal, person


class Acting:
    def __init__(self, api, principal: Principal):
        self._api, self.principal = api, principal
        self.who = Identity(email=principal.email, session=principal.device)     # never who=None
        self.cad = api.cad

    def permit(self, path: str, client: tuple[str, str]):
        """None when the console lets this person POST path; else its 403 (in their activity)."""
        return self._api.permit(path, self.who, None, client=client)

    def post(self, path: str, body: dict, client: tuple[str, str]) -> tuple[int, bytes, str]:
        """The console's write, as this person (CadApi.post: role, operator, validation, activity)."""
        return self._api.post(path, json.dumps(body).encode(), self.who, client)


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
