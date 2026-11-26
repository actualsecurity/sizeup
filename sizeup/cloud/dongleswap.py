"""A dongle swapped for a new one, on the hub: a person's "Use it" and the optional auto-replace (sizeup/radio/replace.py).

The station works out the offers (App.replace_offers, its sdr state's "replace"); the hub shows them on Health and the
console and sends a person's answer back as a replace_sdr job (HubApp.replace_sdr). Nothing moves by itself, with one
exception an admin turns on: Settings > Radios > Receivers, "auto-replace for critical channels" (CAD settings
dongle_swap/auto_critical, off by default). With it on, an offer for the county's trunk-recorder job (Fire/EMS 15) or
paging whose fit is unambiguous (exactly one new dongle fits, and it fits no other gone dongle: the station's "only")
is taken once the old dongle has been gone swap.MISSING_S, and the status topic is told what was done. Every move,
a person's or this, is audited (settings_audit, Radios) and logged.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from sizeup.radio.replace import MISSING_S

TICK_S = 5.0
RETRY_S = 300.0              # an auto move the station refused: tried again this much later


def _list(v) -> list:
    """v when it is a list, else none (a value of another type in a station's status: an older or broken station's)."""
    return v if isinstance(v, list) else []


class AutoReplace:
    """auto_critical's driver: tick() each TICK_S (run()), on the hub only."""

    def __init__(self, hub, cad, notifier=None, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time, public_url: str = ""):
        self.hub, self.cad, self.notifier, self.log, self.clock = hub, cad, notifier, log, clock
        self.public_url = public_url.rstrip("/")
        self._tried: dict[tuple[str, str], float] = {}

    def on(self) -> bool:
        return getattr(self.cad, "dongle_auto_replace", False) is True

    def offers(self) -> list[dict]:
        sdr = self.hub.sdr() if hasattr(self.hub, "sdr") else None
        rows = sdr.get("replace") if isinstance(sdr, dict) else None
        return [o for o in _list(rows) if isinstance(o, dict)]

    def tick(self, now: float | None = None) -> list[tuple[str, str]]:
        """Take every offer auto-replace may take now: the (gone, new) pairs moved."""
        now = self.clock() if now is None else now
        if not self.on():
            return []
        done = []
        for o in self.offers():
            gone, choices = str(o.get("missing") or ""), [c for c in _list(o.get("choices")) if isinstance(c, dict)]
            if not (o.get("critical") and o.get("only") and len(choices) == 1 and gone):
                continue                                 # (ambiguous, or not the county's / paging: a person decides)
            absent = o.get("absent_s")
            if not isinstance(absent, (int, float)) or isinstance(absent, bool) or absent < MISSING_S:
                continue
            new = str(choices[0].get("serial") or "")
            if not new or now - self._tried.get((gone, new), -1e18) < RETRY_S:
                continue
            self._tried[(gone, new)] = now
            jobs = ", ".join(str(j) for j in _list(o.get("jobs"))) or "its jobs"
            try:
                err = self.hub.remote("replace_sdr", gone, new, "use", "auto")
            except Exception as e:  # noqa: BLE001 - offline or refused: tried again RETRY_S later
                err = str(e) or type(e).__name__
            if err:
                self.log(f"dongle swap: auto-replace of SDR {gone} by SDR {new} ({jobs}) refused: {str(err)[:200]}")
                continue
            done.append((gone, new))
            self.log(f"dongle swap: auto-replaced SDR {gone} by SDR {new}: {jobs}")
            self._audit(gone, new, jobs)
            self._tell(o, new, jobs)
        return done

    def _audit(self, gone: str, new: str, jobs: str) -> None:
        try:
            from sizeup.cad.radios import audit
            with self.cad.store.tx() as db:
                audit(db, self.clock(), "auto-replace", "radios", f"sdr_replace:{gone}", f"SDR {gone} ({jobs})",
                      f"SDR {new}")
        except Exception as e:  # noqa: BLE001 - never in the way of the move: said in the log
            self.log(f"dongle swap: audit not written: {type(e).__name__}: {e}")

    def _tell(self, o: dict, new: str, jobs: str) -> None:
        n = self.notifier
        if n is None or not getattr(n, "enabled", False):
            return
        c = next((x for x in _list(o.get("choices")) if isinstance(x, dict)), {})
        name = c.get("name") or new
        click = f"{self.public_url}/cad#settings/radios/receivers?sdr={new}" if self.public_url else ""
        n.send(f"Auto-replaced SDR {o.get('name') or o.get('missing')}",
               f"{jobs} moved to new SDR {name}" + (f" ({c['product']})" if c.get("product") else "")
               + f" · gone SDR {o.get('missing')}" + (f" out of group {o['group']}" if o.get("group") else "") + ".",
               priority=4, tags=("arrows_counterclockwise", "sizeup"), click=click,
               who={"dongle": f"SDR {name}", "channel": jobs})

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the driver must never die
                self.log(f"dongle swap: {type(e).__name__}: {e}")
