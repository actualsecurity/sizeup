"""Reminders: a call or a unit left too long in one state flashes on every console ("hey, are you forgetting something").

A department member, through the admin: "if a call sits in pending for longer than 5 minutes it starts flashing, or if a
unit sits DP for longer than 5 minutes it flashes". Rules (Settings > Dispatch > Reminders; section "reminders", admins,
audited as Dispatch):

    pending      an emergency event in Pending (accepted, no unit on it) for min minutes        on, 5 min
    dispatched   a unit in the dispatched status (DP; a third party's Dispatch other too when the
                 units take them in) on a call for min minutes, not responding / on location     on, 5 min
    queued       a page from 60 Control not accepted for min minutes                              off, 3 min
    onscene      a unit on location for min minutes with no status change (a welfare check)       off, 30 min
    until        an open event min minutes past its Until                                         off, 0 min

    keys: <rule>_on (true | false), <rule>_min (minutes), units (fire_ems | agencies | all: which units the two unit
    rules watch: our rigs, mutual aid and EMS; + other agencies; + third parties, keyholders, utilities, police, with
    their Dispatch other), snooze_min (how long an OK quiets one).

"Since" is the hub's record, never a page load: an event's accept, its last unit cleared, its reopening (whichever is
latest), a page's arrival, a unit's status change (or its move onto this call), the Until. Each reminder comes in the
state as {key, kind, iid, unit, status, since, due}: due is since + the rule's minutes, or later while it is snoozed. The
pages compare due with the hub's clock (the state's time: static/reminders.js), so a wrong PC clock neither starts nor
stops one, and the flash needs no state push of its own.

OK (POST /api/cad/reminders/ack {key, operator}: dispatchers and admins) snoozes that one reminder for everyone for
snooze_min, written in the event's chronology ("Reminder OK: E60 DP 6 min", by whom) and in the person's activity. A
snooze belongs to that spell (its since): once the condition clears, the next one starts fresh. Snoozes kept a day.
"""

from __future__ import annotations

from sizeup.cad import store as store_mod      # (store.py makes the table: SCHEMA, read at its open)

SECTION = "reminders"
SCHEMA = """CREATE TABLE IF NOT EXISTS reminder_snoozes (
  key TEXT PRIMARY KEY,                 -- pending:<incident id> | queued:<id> | until:<id> | dispatched:<unit id> | onscene:<unit id>
  since REAL NOT NULL,                  -- the spell it quiets (a new spell: not snoozed)
  until REAL NOT NULL,
  by TEXT NOT NULL,
  at REAL NOT NULL
)"""
# rule -> (label, on by default, minutes by default, least minutes)
RULES = {
    "pending": ("Pending, no unit", True, 5, 1),
    "dispatched": ("Unit dispatched, not moving", True, 5, 1),
    "queued": ("Page not accepted", False, 3, 1),
    "onscene": ("On scene, no status change", False, 30, 1),
    "until": ("Past its Until", False, 0, 0),
}
INCIDENT_RULES = ("pending", "queued", "until")
MAX_MIN = 240
UNITS = {"fire_ems": "Fire & EMS", "agencies": "+ other agencies", "all": "+ third party (DO)"}
UNITS_DEFAULT = "fire_ems"
SNOOZE_DEFAULT, SNOOZE_MAX = 5, 60
KEEP_S = 86400                           # a snooze row past its until this long: dropped (with the next OK)
EARLY_S = 5.0                            # an OK this much before the hub's due (a page's clock a moment ahead): taken


def keys() -> tuple[str, ...]:
    return (*(f"{r}_{x}" for r in RULES for x in ("on", "min")), "units", "snooze_min")


def default(key: str):
    rule, _, what = key.rpartition("_")
    if key == "units":
        return UNITS_DEFAULT
    if key == "snooze_min":
        return SNOOZE_DEFAULT
    if rule in RULES and what in ("on", "min"):
        return RULES[rule][1] if what == "on" else RULES[rule][2]
    raise ValueError(f"key: one of {', '.join(keys())}")


def clean(key: str, value):
    """The value as stored, or ValueError (the reason)."""
    d = default(key)
    if isinstance(d, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key}: true or false")
        return value
    if key == "units":
        if value not in UNITS:
            raise ValueError(f"units: {', '.join(UNITS)}")
        return value
    lo, hi = (1, SNOOZE_MAX) if key == "snooze_min" else (RULES[key.rpartition("_")[0]][3], MAX_MIN)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not lo <= value <= hi:
        raise ValueError(f"{key}: whole minutes, {lo} to {hi}")
    return int(value)


def config(ov: dict | None) -> dict:
    """The settings as used: {rules: {rule: {on, min}}, units, snooze_min}; a stored value that no longer checks out
    (an older build's): the default."""
    ov = ov or {}

    def get(k):
        try:
            return clean(k, ov[k]) if k in ov else default(k)
        except ValueError:
            return default(k)
    return {"rules": {r: {"on": get(f"{r}_on"), "min": get(f"{r}_min")} for r in RULES},
            "units": get("units"), "snooze_min": get("snooze_min")}


def page(ov: dict | None) -> dict:
    """Settings > Dispatch > Reminders: each value, its default, edited or not."""
    ov, cur = ov or {}, config(ov)

    def item(k, v):
        return {"key": k, "value": v, "default": default(k), "edited": k in ov}
    return {"rules": [{"rule": r, "label": lab, "on": item(f"{r}_on", cur["rules"][r]["on"]),
                       "min": dict(item(f"{r}_min", cur["rules"][r]["min"]), lo=lo, hi=MAX_MIN)}
                      for r, (lab, _, _, lo) in RULES.items()],
            "units": dict(item("units", cur["units"]), choices=[[k, v] for k, v in UNITS.items()]),
            "snooze_min": dict(item("snooze_min", cur["snooze_min"]), lo=1, hi=SNOOZE_MAX)}


def in_scope(u: dict, units: str) -> bool:
    """Does the units setting take this unit in? Ours, mutual aid and EMS always; other agencies from "agencies";
    third parties (keyholders, utilities, police) only with "all"."""
    if u.get("third_party"):
        return units == "all"
    if u.get("response") == "agency" and u.get("kind") != "ems":
        return units in ("agencies", "all")
    return True


def pending_since(inc: dict) -> float:
    """When an emergency event became Pending with no unit: accepted, its last unit cleared or reopened, the latest."""
    at = [inc.get("accepted_at") or inc.get("created_at") or 0.0]
    at += [a["cleared_at"] for a in inc.get("assignments") or () if a.get("cleared_at")]
    at += [e["at"] for e in inc.get("events") or () if e.get("kind") == "reopened"]
    return float(max(at))


def candidates(cfg: dict, incs: list[dict], units: list[dict], lists) -> list[dict]:
    """Every reminder whose condition holds now, due or not yet: {key, kind, iid, number, unit, status, since, after}."""
    R, out = cfg["rules"], []

    def add(kind, since, iid, number, unit="", status="", uid=None):
        out.append({"key": f"{kind}:{iid if uid is None else uid}", "kind": kind, "iid": iid, "number": number or "",
                    "unit": unit, "uid": uid, "status": status, "since": float(since), "after": R[kind]["min"] * 60})
    for inc in incs:
        st, iid, num = inc.get("status"), inc["id"], inc.get("number")
        if st == "pending" and inc.get("emergency") and R["pending"]["on"] \
                and not any(not a.get("cleared_at") for a in inc.get("assignments") or ()):
            add("pending", pending_since(inc), iid, num)
        if st == "queued" and R["queued"]["on"]:
            add("queued", inc["created_at"], iid, num)
        if st in ("pending", "active") and inc.get("until_at") and R["until"]["on"]:
            add("until", inc["until_at"], iid, num)
    dp_on, os_on = R["dispatched"]["on"], R["onscene"]["on"]
    if dp_on or os_on:
        tp_only = set(lists.third_party_only)
        for u in units:
            if not u.get("incident_id") or u.get("placeholder") or not in_scope(u, cfg["units"]):
                continue
            role = lists.role(u["status"])
            if dp_on and (role == "dispatched" or (u.get("third_party") and u["status"] in tp_only)):
                # a move onto this call while still dispatched counts from the move
                add("dispatched", max(u["status_at"], u.get("assigned_at") or 0.0), u["incident_id"], u.get("incident_number"),
                    u["name"], u["status"], u["id"])
            elif os_on and role == "onscene":
                add("onscene", u["status_at"], u["incident_id"], u.get("incident_number"), u["name"], u["status"], u["id"])
    return out


def chip_text(item: dict, now: float) -> str:
    """"Pending 6 min", "E60 DP 6 min" (the chronology's words; the pages build their own, static/reminders.js)."""
    m = max(0, int((now - item["since"]) // 60))
    head = {"pending": "Pending", "queued": "Not accepted", "until": "Past Until"}.get(item["kind"])
    return f"{head} {m} min" if head else f"{item['unit']} {item['status']} {m} min"


class Reminders:
    """One per Cad (sizeup/cad/service.py): the state's reminders and the shared OK."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store

    def cfg(self) -> dict:
        return config(self.cad.settings.section(SECTION))

    def _snoozes(self) -> dict[str, dict]:
        return {r["key"]: r for r in self.store.q("SELECT key, since, until, by FROM reminder_snoozes")}

    def items(self, incs: list[dict], units: list[dict], cfg: dict | None = None) -> list[dict]:
        cfg = cfg or self.cfg()
        snz = self._snoozes()
        out = []
        for c in candidates(cfg, incs, units, self.cad.lists):
            due = c["since"] + c["after"]
            s = snz.get(c["key"])
            it = {"key": c["key"], "kind": c["kind"], "iid": c["iid"], "unit": c["unit"], "status": c["status"],
                  "since": c["since"], "due": due}
            if s and abs(s["since"] - c["since"]) < 0.001 and s["until"] > due:      # this spell, snoozed
                it.update(due=s["until"], by=s["by"])
            out.append(it)
        return out

    def state(self, incs: list[dict], units: list[dict]) -> dict:
        """The state's "reminders" (built with the rest of it, Cad.state): the rules the pages need and the items."""
        cfg = self.cfg()
        try:
            items = self.items(incs, units, cfg)
        except Exception as e:  # noqa: BLE001 - never breaks the console
            self.cad.log(f"CAD: reminders: {type(e).__name__}: {e}")
            items = []
        return {"snooze_min": cfg["snooze_min"], "items": items}

    def ack(self, body: dict) -> dict:
        """POST /api/cad/reminders/ack {key, operator}: snooze_min of quiet for that reminder, for everyone."""
        key, op = body.get("key"), body.get("operator")
        if not isinstance(key, str) or not key or len(key) > 40:
            raise store_mod.CadError("key: the reminder")
        if not isinstance(op, str) or not op.strip():
            raise store_mod.CadError("operator: who is acknowledging (enter your name)")
        op = op.strip()[:store_mod.OPERATOR_MAX]
        cfg = self.cfg()
        now = self.store.clock()
        c = next((x for x in candidates(cfg, self.store.incidents(), self.store.units(), self.cad.lists) if x["key"] == key), None)
        if c is None:
            raise store_mod.CadError("that reminder has cleared")
        if now < c["since"] + c["after"] - EARLY_S:
            raise store_mod.CadError("that reminder is not due yet")
        text = chip_text(c, now)
        s = self.store.one("SELECT since, until, by FROM reminder_snoozes WHERE key = ?", (key,))
        if s and abs(s["since"] - c["since"]) < 0.001 and s["until"] > now:      # someone else just did: theirs stands
            return {"ok": True, "key": key, "until": s["until"], "by": s["by"], "already": True, "text": text,
                    "incident_number": c["number"]}
        until = now + cfg["snooze_min"] * 60
        with self.store.tx() as db:
            db.execute("DELETE FROM reminder_snoozes WHERE until < ?", (now - KEEP_S,))
            db.execute("INSERT INTO reminder_snoozes (key, since, until, by, at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO "
                       "UPDATE SET since = excluded.since, until = excluded.until, by = excluded.by, at = excluded.at",
                       (key, c["since"], until, op, now))
            self.store._event(db, c["iid"], "system", f"Reminder OK: {text} · quiet {cfg['snooze_min']} min", op, c["uid"], now)
        return {"ok": True, "key": key, "until": until, "by": op, "text": text, "incident_number": c["number"]}
