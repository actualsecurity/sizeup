"""New radio IDs heard on the fireground (#163): "Radio 1234 (MDC) on FG1: who is this?"

An FG1 transmission goes on an event only from a radio the registry names (sizeup/cad/comms.py, why "unit");
one with no ID stays in the Radio view, and one with an ID nobody has named yet is asked about here, never
guessed. The state's "radio_ids" (Cad.state) lists them, newest first:

    asked     an MDC or FleetSync ID decoded on a tactical channel (FG1: Settings' traffic channels) in the last
              ASK_S, with no unit in the registry and no person's answer (sizeup/cad/radios.py: a row not edited;
              heard() makes the row, source "heard", the first time it is decoded)
    with it   how many times and when it was heard (first / last), its newest transmission (to play), and the
              emergency events that had units on them when it was last heard (comms.ACTIVE_EVENTS): "during
              26-000016", their units offered first
    answer    POST /api/cad/radio-id {system, radio_id, unit?, label?, ignore?, operator}, dispatchers and admins:
                unit    the registry row gets it (source confirmed, an edit like Settings > Radios', in its log), and
                        its transmissions already logged go on the events that unit was on (Comms.relink)
                label   who it is with no unit ("Chief Smith's portable", "Rye PD"): kept, nothing is linked
                ignore  not one of ours: the row is marked edited with no unit, so it is never asked again (and,
                        as for any row a person set to no rig, never linked)
              an ID someone already named (a unit, or edited) is not answered here: Settings > Radios

Cheap: read only when something changed (the store, the registry) or once a minute (ASK_S running out), and
nothing in it moves by itself between those, so the push (#146: changed sections only) sends it only then.
"""

from __future__ import annotations

from sizeup.cad.radios import FS_ID, MDC_ID, RadioError, fm_system

ASK_S = 30 * 60          # an unnamed ID heard this recently is asked about
MAX_ASKED = 12
KIND = {"mdc": "MDC", "fleetsync": "FleetSync"}


class NewIds:
    def __init__(self, cad):
        self.cad = cad
        self._cache: tuple[tuple, list[dict]] | None = None

    # -- the state ---------------------------------------------------------------------------------

    def state(self, now: float) -> list[dict]:
        key = (self.cad.store.rev, self.cad.radios.rev, int(now // 60))
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, self._asked(now))
        return self._cache[1]

    def _asked(self, now: float) -> list[dict]:
        chans = sorted(self.cad.comms.tactical)
        if not chans:
            return []
        rows = self.cad.store.q(
            f"SELECT id, start, channel, radio_id FROM radio_traffic WHERE talkgroup IS NULL AND radio_id != '' "
            f"AND rig = '' AND start >= ? AND channel IN ({','.join('?' * len(chans))}) ORDER BY start DESC LIMIT 500",
            (now - ASK_S, *chans))
        by: dict[tuple[str, str], dict] = {}
        for r in rows:
            for rid in [x.strip() for x in r["radio_id"].split(",") if x.strip()]:
                system = fm_system(rid)
                if not (FS_ID.fullmatch(rid) or MDC_ID.fullmatch(rid.upper())):
                    continue
                key = (system, rid.upper() if system == "mdc" else rid)
                a = by.get(key)
                if a is None:
                    reg = self.cad.radios.get(*key)
                    if reg is not None and (reg["unit"] or reg["edited"]):
                        continue            # named, or a person said: on no rig
                    a = by[key] = {"key": f"{key[0]}:{key[1]}", "system": key[0], "radio_id": key[1], "kind": KIND[key[0]],
                                   "channel": r["channel"], "last_at": r["start"], "first_at": r["start"], "count": 0,
                                   "traffic_id": r["id"], "label": (reg or {}).get("label", "")}
                a["count"] += 1
                a["first_at"] = min(a["first_at"], r["start"])
        out = sorted(by.values(), key=lambda a: -a["last_at"])[:MAX_ASKED]
        for a in out:
            a["incidents"] = self._during(a["last_at"])
        return out

    def _during(self, at: float) -> list[dict]:
        """The emergency events with units on them at that time (their units: what to offer first)."""
        from sizeup.cad.comms import Comms
        ids = [r["incident_id"] for r in self.cad.store.q(Comms.ACTIVE_EVENTS, (at, at))]
        if not ids:
            return []
        incs = self.cad.store.q(f"SELECT id, number, type, subtype, address FROM incidents WHERE id IN "
                                f"({','.join('?' * len(ids))}) AND status NOT IN ('closed', 'dismissed') ORDER BY id", ids)
        for i in incs:
            i["units"] = [r["name"] for r in self.cad.store.q(
                "SELECT DISTINCT u.name FROM assignments a JOIN units u ON u.id = a.unit_id WHERE a.incident_id = ? "
                "AND a.assigned_at IS NOT NULL AND a.assigned_at <= ? AND (a.cleared_at IS NULL OR a.cleared_at >= ?) "
                "ORDER BY u.name", (i["id"], at, at))]
        return incs

    # -- the answer --------------------------------------------------------------------------------

    def answer(self, body: dict, operator: str = "") -> dict:
        """{system, radio_id, unit?, label?, ignore?}: who an asked-about radio is (see the top). Returns
        {radio, linked: [event numbers]}."""
        reg = self.cad.radios
        system = str(body.get("system") or "")
        if system not in KIND:
            raise RadioError("system: mdc or fleetsync")
        rid = str(body.get("radio_id") or "").strip()
        rid = rid.upper() if system == "mdc" else rid
        old = reg.get(system, rid)
        if old is not None and (old["unit"] or old["edited"]):
            raise RadioError(f"{KIND[system]} {rid} is already set ({old['unit'] or 'no rig'}): change it in Settings > Radios")
        unit = str(body.get("unit") or "").strip()
        label = " ".join(str(body.get("label") or "").split())
        ignore = body.get("ignore") is True
        if ignore and unit:
            raise RadioError("ignore, or a unit: not both")
        if not ignore and not unit and not label:
            raise RadioError("who is it: a unit, a name, or ignore")
        f = {"system": system, "radio_id": rid, "unit": "" if ignore else unit, "label": label or (old or {}).get("label", "")}
        if ignore:
            f["notes"] = (old or {}).get("notes") or "Ignored on the console: not one of ours"
        elif unit:
            f["source"] = "confirmed"
        row = reg.save(f, operator)
        linked = self.cad.comms.relink(system, rid, row["unit"]) if row["unit"] else []
        nums = [r["number"] or str(r["id"]) for r in self.cad.store.q(
            f"SELECT id, number FROM incidents WHERE id IN ({','.join('?' * len(linked))}) ORDER BY id", linked)] if linked else []
        self._cache = None
        return {"radio": row, "linked": nums}
