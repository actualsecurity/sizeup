"""Notifications: their sub-type, and the hydrants out of service they name (Notification / <subtype> · Hydrants OOS).

A Notification (the NOTIF type, any subtype: Lists.is_notification) is non-emergency. What it is about is its sub-type
(Settings > Lists > Notification sub-types: Hydrants OOS, Hydrants back in service, Road closure, Water main, Other),
shown after its type on the queue and the event, as an Airport event's alert is. It comes from the admins' rules, or
is set by hand on the event (a person's choice stands: the rules never change it back).

The rules (Settings > Lists > Notification rules, admins; settings_overrides section SECTION, key "rules"): an ordered
list, each

    name         what the admins call it
    match        a regular expression (any case) looked for in the notification's text: its remarks, each comment a
                 feed sent ("<feed> comment: ..." on its timeline) and each note a person wrote
    kind         the sub-type it sets (the first rule that matches sets it); "" none
    hydrants     "oos" | "in" | "": the hydrant numbers it names are out of service, or back in service
    numbers      a regular expression for one hydrant number ("" : NUMBERS), read inside the match's group "nums", else
                 its first group, else the whole match ("HYD (?P<nums>[0-9, ]+) OOS" on "HYD 12, 34 OOS": 12 and 34)
    auto_apply   false (the default): a person applies what is proposed. true: what matches one hydrant near the call
                 is applied at once (never a number found elsewhere, never one not in the hydrant list)
    on           false: kept, not used

None is in the repo: the department's comment formats are the admins' to enter. key "auto_close" (default false): a
notification closes by itself once every hydrant linked to it is back in service; off, it asks (Close).

Each number a rule finds is a row (notif_hydrants), matched to the hydrant list (sizeup/cad/hydrants.py by_number: the
number shown, the county's number or the asset id, leading zeros aside):

    near         one hydrant with that number within NEAR_M of the notification's point: Mark OOS (in service), or Link
                 (already out of service: marked by hand, nothing changed on it)
    elsewhere    none near it, one or more further away (or the notification has no point): listed with the distance
                 and where; a person confirms which one (Confirm)
    none         not in the hydrant list: "not in the hydrant list"; admins get Add (a hydrant at the number)

Apply (admins, as every hydrant edit): the hydrant marked out of service with "Notification <number>" as the reason,
audited in the Settings audit log (section "hydrants", its history) and on the notification's timeline, and linked.
A linked hydrant keeps its notification open: the county's in service (sizeup/cad/automode.py), an Until
(Store.expire_until) never close it, a person's Close asks first. Back in service (the event's action, the hydrant's own
card, or a later "in" notification applied) releases the link; the last one released asks Close, or closes it (auto
close). Every hydrant card says which notification holds it.
"""

from __future__ import annotations

import math
import re
import threading

from sizeup.cad.store import OPEN, CadError

SECTION = "notif_rules"                      # settings_overrides: "rules" -> [rule], "auto_close" -> bool
KINDS = "notification_kinds"                 # Settings > Lists (sizeup/cad/lists.py)
HYD_OOS, HYD_IN = "HYD-OOS", "HYD-IS"         # the seeded sub-types the hydrant rules set
ACTIONS = {"": "", "oos": "Hydrants OOS", "in": "Hydrants back in service"}
DOWN = ("oos", "dry")                        # sizeup/cad/hydrants.py DOWN
NUMBERS = r"\b[A-Z]?\d{1,6}[A-Z]?\b"         # one hydrant number when a rule gives none ("12", "A12", "12B")
NEAR_M = 600.0                               # a number's hydrant this close to the notification: that one
MAX_RULES, PATTERN_MAX, NAME_MAX = 50, 300, 60
MAX_NUMBERS = 40                             # numbers per notification at most (a runaway pattern)
TEXT_MAX = 4000                              # each text read at most this long
CANDS = 5                                    # hydrants elsewhere offered per number
AUTO = "Hydrants · auto"                     # who closes a notification when the last hydrant is back (auto_close)
STATES = ("proposed", "linked", "back", "done", "dismissed")
# a group with a repeat inside, itself repeated ("(a+)+", "(\\d+,?)*", "(?:x|y*){2,}"): catastrophic backtracking on a long
# text (Python's re holds the GIL: one such rule would stall the hub on every comment), refused when saved
NESTED = re.compile(r"\((?:[^()\\]|\\.)*(?:[+*]|\{\d*,\d*\})(?:[^()\\]|\\.)*\)(?:[+*]|\{\d*,\d*\})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notif_incidents (
  incident_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT '',        -- a notification_kinds code
  kind_by TEXT NOT NULL DEFAULT '',     -- '' : a rule's; else who set it by hand (it stands)
  rule TEXT NOT NULL DEFAULT '',        -- the rule's name that set it
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notif_hydrants (
  id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL,
  number TEXT NOT NULL,                 -- as the notification has it ("12")
  action TEXT NOT NULL,                 -- oos | in: what it says of that hydrant
  rule TEXT NOT NULL DEFAULT '',
  hydrant_key TEXT NOT NULL DEFAULT '', -- the hydrant (picked, applied or linked); '' : matched when shown
  state TEXT NOT NULL DEFAULT 'proposed',   -- proposed | linked (holds it open) | back | done (an "in" applied) | dismissed
  found_at REAL NOT NULL,
  linked_at REAL, linked_by TEXT NOT NULL DEFAULT '',
  back_at REAL, back_by TEXT NOT NULL DEFAULT '',
  UNIQUE (incident_id, number, action)
);
CREATE INDEX IF NOT EXISTS notif_hydrants_key ON notif_hydrants(hydrant_key);
"""


# ---- pure helpers ----------------------------------------------------------------------------------------------------

def num_key(v) -> str:
    """A hydrant number as compared: letters and digits, upper case, leading zeros off ('012' -> '12', 'p-1' -> 'P1')."""
    k = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
    return re.sub(r"^0+(?=\d)", "", k)


def _regex(v, name: str, n: int = PATTERN_MAX) -> str:
    if not isinstance(v, str) or len(v) > n:
        raise ValueError(f"{name}: a regular expression, at most {n} characters")
    v = v.strip()
    try:
        rx = re.compile(v, re.I)
    except re.error as e:
        raise ValueError(f"{name}: {e}") from None
    if rx.search("") is not None:
        raise ValueError(f"{name}: matches nothing at all (empty text): too broad")
    if NESTED.search(v):
        raise ValueError(f"{name}: a repeated group that repeats inside, as (a+)+: could take forever on a long comment")
    return v


def clean_rule(raw, kinds=None) -> dict:
    """A rule as stored (ValueError with the reason). kinds: the sub-type codes allowed (None: not checked)."""
    if not isinstance(raw, dict):
        raise ValueError("rule: an object")
    name = " ".join(str(raw.get("name") or "").split())
    if not name or len(name) > NAME_MAX:
        raise ValueError(f"name: 1 to {NAME_MAX} characters")
    match = _regex(raw.get("match"), "match")
    if not match:
        raise ValueError("match: required")
    numbers = _regex(raw.get("numbers") or "", "numbers", 120) if raw.get("numbers") else ""
    kind = str(raw.get("kind") or "").strip().upper()
    if kind and kinds is not None and kind not in kinds:
        raise ValueError(f"kind: {kind} is not a notification sub-type")
    hyd = str(raw.get("hydrants") or "")
    if hyd not in ACTIONS:
        raise ValueError("hydrants: oos, in or none")
    if not kind and not hyd:
        raise ValueError("a sub-type, hydrants, or both")
    auto = raw.get("auto_apply", False)
    on = raw.get("on", True)
    if not isinstance(auto, bool) or not isinstance(on, bool):
        raise ValueError("auto_apply, on: true or false")
    if auto and not hyd:
        raise ValueError("auto_apply: only for a hydrants rule")
    rid = str(raw.get("id") or "")
    if rid and not re.fullmatch(r"r[1-9]\d{0,5}", rid):
        raise ValueError("id: r<n>")
    return {"id": rid, "name": name, "match": match, "numbers": numbers, "kind": kind, "hydrants": hyd,
            "auto_apply": auto, "on": on}


def check_section(sec) -> dict:
    """The section as a settings file has it (an import): {rules: [...], auto_close} checked (ValueError)."""
    if not isinstance(sec, dict) or set(sec) - {"rules", "auto_close"}:
        raise ValueError("rules and auto_close only")
    out = {}
    if "auto_close" in sec:
        if not isinstance(sec["auto_close"], bool):
            raise ValueError("auto_close: true or false")
        out["auto_close"] = sec["auto_close"]
    if "rules" in sec:
        rules = sec["rules"]
        if not isinstance(rules, list) or len(rules) > MAX_RULES:
            raise ValueError(f"rules: a list of at most {MAX_RULES}")
        got, ids = [], set()
        for i, r in enumerate(rules):
            try:
                c = clean_rule(r)
            except ValueError as e:
                raise ValueError(f"rules[{i}].{e}") from None
            if not c["id"] or c["id"] in ids:
                raise ValueError(f"rules[{i}].id: r<n>, once")
            ids.add(c["id"])
            got.append(c)
        out["rules"] = got
    return out


def config(sec, lists=None) -> dict:
    """The section as used: {rules: [rule + rx, nrx, off (why not used)], auto_close}. Never raises (a rule that no
    longer compiles is off)."""
    sec = sec if isinstance(sec, dict) else {}
    codes = {k["code"] for k in getattr(lists, KINDS, ()) or () if not k.get("retired")} if lists is not None else None
    rules = []
    for r in sec.get("rules") or []:
        try:
            c = clean_rule(r)
            c["rx"] = re.compile(c["match"], re.I)
            c["nrx"] = re.compile(c["numbers"] or NUMBERS, re.I)
        except (ValueError, re.error):
            continue
        c["off"] = "" if c["on"] else "off"
        if c["kind"] and codes is not None and c["kind"] not in codes:
            c["kind_off"] = f"{c['kind']}: not a sub-type now"
        rules.append(c)
    return {"rules": rules, "auto_close": sec.get("auto_close") is True}


def numbers_in(rule: dict, m: re.Match) -> list[str]:
    """The hydrant numbers one match names: in its group "nums", else its first group, else the whole match."""
    rx = rule["rx"]
    region = m.group("nums") if "nums" in rx.groupindex else (m.group(1) if rx.groups else m.group(0))
    out = []
    for x in rule["nrx"].finditer(region or ""):
        n = (x.group(1) if rule["nrx"].groups else x.group(0)) or ""
        n = " ".join(n.split()).upper()
        if n and num_key(n) and n not in out:
            out.append(n)
    return out


def read(cfg: dict, texts: list[str]) -> dict:
    """What the rules make of these texts: {kind, rule (its name), found: [(number, action, rule)]}."""
    kind, by, found, seen = "", "", [], set()
    for r in cfg["rules"]:
        if not r["on"]:
            continue
        for t in texts:
            ms = list(r["rx"].finditer(t[:TEXT_MAX]))
            if not ms:
                continue
            if not kind and r["kind"] and not r.get("kind_off"):
                kind, by = r["kind"], r["name"]
            if r["hydrants"]:
                for m in ms:
                    for n in numbers_in(r, m):
                        k = (num_key(n), r["hydrants"])
                        if k not in seen and len(found) < MAX_NUMBERS:
                            seen.add(k)
                            found.append((n, r["hydrants"], r))
    return {"kind": kind, "rule": by, "found": found}


def page(sec, lists) -> dict:
    """Settings > Lists > Notification rules: the rules (each with why it isn't used, if so) and the switch."""
    cfg = config(sec, lists)
    stored = {r.get("id"): r for r in ((sec or {}).get("rules") or []) if isinstance(r, dict)}
    rows = []
    for r in cfg["rules"]:
        rows.append({k: r[k] for k in ("id", "name", "match", "numbers", "kind", "hydrants", "auto_apply", "on")}
                    | {"off": r.get("kind_off", "")})
    bad = [r for k, r in stored.items() if k not in {x["id"] for x in rows}]
    return {"rules": rows, "broken": len(bad), "auto_close": cfg["auto_close"], "numbers": NUMBERS,
            "actions": [[k, v] for k, v in ACTIONS.items() if k]}


def _metres(lat1, lon1, lat2, lon2) -> float:
    ky, kx = 110_574.0, 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lat2 - lat1) * ky, (lon2 - lon1) * kx)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---- the service -----------------------------------------------------------------------------------------------------

class Notif:
    """One per Cad (sizeup/cad/service.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        self._lock = threading.Lock()                # one scan or apply at a time (two feed reports, a click)
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'incidents'").fetchone():
                self.store.db.executescript(SCHEMA)
        self.store.holds = self.holds                # Store._expire_one: an Until never closes one holding a hydrant

    def cfg(self) -> dict:
        c = getattr(self.cad, "notif_cfg", None)
        return c if c is not None else config({}, self.cad.lists)

    def _hy(self):
        return getattr(self.cad, "hydrants", None)

    def _kind_name(self, code: str) -> str:
        return self.cad.lists.name(KINDS, code) if code else ""

    # -- what holds a notification open ----------------------------------------------------------------------------------

    def holds(self, db, iid: int) -> str:
        """Store._expire_one's hook (in its transaction): "2 hydrants OOS" while hydrants are linked to it, else ''."""
        n = db.execute("SELECT COUNT(*) FROM notif_hydrants WHERE incident_id = ? AND state = 'linked'", (iid,)).fetchone()[0]
        return f"{_plural(n, 'hydrant')} OOS" if n else ""

    def held(self, iid: int) -> int:
        r = self.store.one("SELECT COUNT(*) AS n FROM notif_hydrants WHERE incident_id = ? AND state = 'linked'", (iid,))
        return int(r["n"]) if r else 0

    # -- reading a notification ----------------------------------------------------------------------------------------

    def texts(self, iid: int, inc: dict | None = None) -> list[str]:
        """What the rules read: the remarks, each comment a feed sent, each note a person wrote."""
        inc = inc or self.store.incident(iid)
        out = [inc.get("remarks") or ""]
        for e in self.store.q("SELECT kind, text FROM events WHERE incident_id = ? AND kind IN ('feed', 'note') ORDER BY id",
                              (iid,)):
            t = e["text"] or ""
            if e["kind"] == "feed":
                if " comment: " not in t:
                    continue
                t = t.split(" comment: ", 1)[1]
            out.append(t)
        return [t for t in out if t.strip()]

    def scan(self, iid: int, operator: str = "") -> dict | None:
        """The rules over an open notification: its sub-type (unless set by hand), a row per hydrant number new to it;
        a rule's auto_apply then applies what matches one hydrant near it. Never raises (a feed report, a note)."""
        try:
            return self._scan(iid)
        except Exception as e:  # noqa: BLE001 - never the caller's write
            from sizeup.fence import FencedWrite
            if isinstance(e, FencedWrite):
                raise
            self.cad.log(f"CAD: notification rules: {type(e).__name__}: {e}")
            return None

    def _scan(self, iid: int) -> dict | None:
        cfg = self.cfg()
        if not any(r["on"] for r in cfg["rules"]):
            return None
        inc = self.store.incident(iid)
        if inc["status"] not in OPEN or not self.cad.lists.is_notification(inc["type"]):
            return None
        got = read(cfg, self.texts(iid, inc))
        if not got["kind"] and not got["found"]:
            return None
        now = self.cad.clock()
        new: list[tuple[int, dict]] = []
        with self._lock:
            with self.store.tx() as db:
                cur = db.execute("SELECT * FROM notif_incidents WHERE incident_id = ?", (iid,)).fetchone()
                if got["kind"] and (cur is None or (not cur["kind_by"] and cur["kind"] != got["kind"])):
                    db.execute("INSERT INTO notif_incidents (incident_id, kind, kind_by, rule, at) VALUES (?, ?, '', ?, ?) "
                               "ON CONFLICT(incident_id) DO UPDATE SET kind = excluded.kind, rule = excluded.rule, at = excluded.at",
                               (iid, got["kind"], got["rule"], now))
                    self.store._event(db, iid, "system", f"Sub-type: {self._kind_name(got['kind'])} ({got['rule']})",
                                      at=now)
                named: dict[str, list[str]] = {}
                for n, act, r in got["found"]:
                    have = db.execute("SELECT number FROM notif_hydrants WHERE incident_id = ? AND action = ?",
                                      (iid, act)).fetchall()
                    if any(num_key(h[0]) == num_key(n) for h in have):
                        continue
                    c = db.execute("INSERT INTO notif_hydrants (incident_id, number, action, rule, found_at) "
                                   "VALUES (?, ?, ?, ?, ?)", (iid, n, act, r["name"], now))
                    new.append((c.lastrowid, r))
                    named.setdefault(act, []).append(n)
                for act, ns in named.items():
                    self.store._event(db, iid, "system", f"{ACTIONS[act]}: {', '.join(ns)} (proposed)", at=now)
        auto: dict[str, list[int]] = {}
        for rid, r in new:
            if r["auto_apply"]:
                auto.setdefault(r["name"], []).append(rid)
        for name, ids in auto.items():
            rows = {x["id"]: x for x in self.rows(self.store.incident(iid))}
            sure = [i for i in ids if rows.get(i, {}).get("match") in ("near", "linked")      # (both near this one)
                    and rows[i]["next"] in ("mark", "link", "back")]
            if sure:
                self.apply(iid, {"items": [{"id": i} for i in sure], "operator": f"{name} · auto"})
        return {"kind": got["kind"], "new": [rid for rid, _ in new]}

    # -- matching each number to the hydrant list ------------------------------------------------------------------------

    def _brief(self, hy, it: dict, here) -> dict:
        r = it["row"] or {}
        d = round(_metres(here[0], here[1], it["lat"], it["lon"])) if here else None
        return {"key": it["key"], "number": it["number"], "status": it["status"], "cls": it["cls"],
                "cls_assumed": hy.assumed(it), "lat": it["lat"], "lon": it["lon"], "dist_m": d, "town": hy.town(it),
                "location": r.get("location") or "", "location_qualifier": r.get("location_qualifier") or "",
                "status_reason": r.get("status_reason") or ""}

    def _held_keys(self, keys) -> dict[str, list[int]]:
        """Hydrant key -> the open notifications holding it (linked)."""
        keys = [k for k in keys if k]
        if not keys:
            return {}
        out: dict[str, list[int]] = {}
        for r in self.store.q(f"SELECT h.hydrant_key AS k, h.incident_id AS i FROM notif_hydrants h JOIN incidents x ON "
                              f"x.id = h.incident_id WHERE h.state = 'linked' AND x.status IN ('queued', 'pending', 'active') "
                              f"AND h.hydrant_key IN ({','.join('?' * len(keys))})", keys):
            out.setdefault(r["k"], []).append(r["i"])
        return out

    def _resolve(self, inc: dict, r: dict, hy) -> dict:
        here = (inc["lat"], inc["lon"]) if inc.get("lat") is not None and inc.get("lon") is not None else None
        out = {"id": r["id"], "number": r["number"], "action": r["action"], "state": r["state"], "rule": r["rule"],
               "key": r["hydrant_key"], "match": "", "next": "", "h": None, "cands": [], "by": _who(r["linked_by"] or r["back_by"])}
        idx = hy.index() if hy is not None else {"by_key": {}}
        if r["hydrant_key"]:
            it = idx["by_key"].get(r["hydrant_key"])
            out["match"] = "picked" if it is not None and not it["removed"] else "gone"
            if it is not None:
                out["h"] = self._brief(hy, it, here)
        elif r["state"] == "proposed" and hy is not None:
            cands = hy.by_number(r["number"])
            held = self._held_keys([c["key"] for c in cands]) if r["action"] == "in" else {}
            near = [c for c in cands if here and _metres(here[0], here[1], c["lat"], c["lon"]) <= NEAR_M]
            mine = [c for c in near if c["key"] in held]           # held by an open notification, near this one
            if r["action"] == "in" and len(mine) == 1:
                out["match"], out["h"] = "linked", self._brief(hy, mine[0], here)   # the one a notification holds
            elif len(near) == 1:
                out["match"], out["h"] = "near", self._brief(hy, near[0], here)
            elif not cands:
                out["match"] = "none"
            else:
                out["match"] = "ambiguous" if near else "elsewhere"
                pool = near or cands
                pool = sorted(pool, key=lambda c: (c["key"] not in held,       # a held one first ("in")
                                                   _metres(here[0], here[1], c["lat"], c["lon"]) if here else 0, c["key"]))
                out["cands"] = [self._brief(hy, c, here) for c in pool[:CANDS]]
                out["more"] = max(0, len(pool) - CANDS)
        h = out["h"]
        down = bool(h) and h["status"] in DOWN
        if r["state"] == "proposed":
            if out["match"] in ("elsewhere", "ambiguous"):
                out["next"] = "confirm"
            elif out["match"] == "none":
                out["next"] = "add" if r["action"] == "oos" else ""
            elif h is not None and out["match"] != "gone":
                out["next"] = ("link" if down else "mark") if r["action"] == "oos" else ("back" if down else "")
        elif r["state"] == "linked":
            out["next"] = "back" if down else ""
        return out

    def rows(self, inc: dict) -> list[dict]:
        hy = self._hy()
        return [self._resolve(inc, r, hy) for r in self.store.q(
            "SELECT * FROM notif_hydrants WHERE incident_id = ? ORDER BY id", (inc["id"],))]

    # -- the state push --------------------------------------------------------------------------------------------------

    def decorate(self, incs: list[dict]) -> None:
        """Each notification's "notif": {kind, kind_name, by, rule, oos (linked), rows (open ones), all_back, linked
        (keys)}; and a call's hydrants marked linked (inc.hydrants.linked)."""
        ids = [i["id"] for i in incs]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        kinds = {r["incident_id"]: r for r in self.store.q(f"SELECT * FROM notif_incidents WHERE incident_id IN ({marks})", ids)}
        hyd: dict[int, list[dict]] = {}
        for r in self.store.q(f"SELECT * FROM notif_hydrants WHERE incident_id IN ({marks}) ORDER BY id", ids):
            hyd.setdefault(r["incident_id"], []).append(r)
        hy = self._hy()
        for inc in incs:
            k, rs = kinds.get(inc["id"]), hyd.get(inc["id"], [])
            if k is None and not rs:
                continue
            linked = [r for r in rs if r["state"] == "linked"]
            d = {"kind": k["kind"] if k else "", "kind_name": self._kind_name(k["kind"]) if k else "",
                 "by": ("hand" if k["kind_by"] else "rule") if k else "", "rule": k["rule"] if k else "",
                 "oos": len(linked), "linked": sorted({r["hydrant_key"] for r in linked if r["hydrant_key"]}),
                 "all_back": not linked and any(r["state"] == "back" for r in rs) and inc["status"] in OPEN}
            if inc["status"] in OPEN and rs:
                try:
                    d["rows"] = [self._resolve(inc, r, hy) for r in rs]
                except Exception as e:  # noqa: BLE001 - never the push
                    self.cad.log(f"CAD: notification hydrants: {type(e).__name__}")
            inc["notif"] = d
            if d["linked"] and isinstance(inc.get("hydrants"), dict):
                inc["hydrants"]["linked"] = d["linked"]

    def for_hydrant(self, key: str) -> list[dict]:
        """A hydrant's card: the notifications that hold it (linked), newest first."""
        return [{"id": r["id"], "number": r["number"] or "", "status": r["status"], "since": r["linked_at"]}
                for r in self.store.q("SELECT x.id, x.number, x.status, h.linked_at FROM notif_hydrants h JOIN incidents x "
                                      "ON x.id = h.incident_id WHERE h.hydrant_key = ? AND h.state = 'linked' "
                                      "ORDER BY h.linked_at DESC LIMIT 5", (key,))]

    # -- a person's actions ----------------------------------------------------------------------------------------------

    def set_kind(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/notif {kind}: the sub-type by hand ("" : the rules' again). Dispatchers, admins."""
        op = str(body.get("operator") or "")[:320]
        inc = self.store.incident(iid)
        if not self.cad.lists.is_notification(inc["type"]):
            raise CadError("a Notification's sub-type")
        kind = str(body.get("kind") or "").strip().upper()
        if kind and kind not in {k["code"] for k in self.cad.lists.notification_kinds if not k.get("retired")}:
            raise CadError(f"kind: {kind} is not a notification sub-type")
        now = self.cad.clock()
        with self.store.tx() as db:
            cur = db.execute("SELECT * FROM notif_incidents WHERE incident_id = ?", (iid,)).fetchone()
            if kind:
                if cur is not None and cur["kind"] == kind and cur["kind_by"]:
                    return {"ok": True}
                db.execute("INSERT INTO notif_incidents (incident_id, kind, kind_by, rule, at) VALUES (?, ?, ?, '', ?) "
                           "ON CONFLICT(incident_id) DO UPDATE SET kind = excluded.kind, kind_by = excluded.kind_by, "
                           "rule = '', at = excluded.at", (iid, kind, op or "?", now))
                self.store._event(db, iid, "edit", f"Sub-type: {self._kind_name(kind)}", op, at=now)
            elif cur is not None:
                db.execute("DELETE FROM notif_incidents WHERE incident_id = ?", (iid,))
                self.store._event(db, iid, "edit", "Sub-type: from the rules", op, at=now)
        if not kind:
            self.scan(iid)
        return {"ok": True}

    def hydrants_op(self, iid: int, body: dict) -> dict:
        """POST /api/cad/incident/<id>/hydrants {action: apply | back | link | dismiss, ...}: admins (hydrant edits)."""
        act = str(body.get("action") or "")
        fn = {"apply": self.apply, "back": self.back, "link": self.link, "dismiss": self.dismiss}.get(act)
        if fn is None:
            raise CadError("action: apply, back, link or dismiss")
        return fn(iid, body)

    def _ids(self, body: dict, key: str = "ids") -> list[int] | None:
        v = body.get(key)
        if v is None:
            return None
        if not isinstance(v, list) or len(v) > MAX_NUMBERS or not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
            raise CadError(f"{key}: a list of row ids")
        return v

    def _reason(self, inc: dict) -> str:
        return f"Notification {inc['number'] or inc['id']}"

    def apply(self, iid: int, body: dict) -> dict:
        """{items?: [{id, key?}]}: each proposed row done: Mark OOS (an in-service hydrant: out of service, the reason the
        notification), Link (already out of service), Back (an "in" row: in service again, its links released); key: the
        hydrant a person confirmed among those offered. None: every row that needs no choice."""
        hy = self._hy()
        if hy is None:
            raise CadError("no hydrant list here")
        op = str(body.get("operator") or "")[:320]
        inc = self.store.incident(iid)
        items = body.get("items")
        if items is not None and (not isinstance(items, list) or len(items) > MAX_NUMBERS or not all(
                isinstance(x, dict) and isinstance(x.get("id"), int) and not isinstance(x.get("id"), bool) for x in items)):
            raise CadError("items: [{id, key?}]")
        rows = {r["id"]: r for r in self.rows(inc)}
        picks = {x["id"]: str(x.get("key") or "") for x in items} if items is not None else None
        todo = []
        for rid, r in rows.items():
            if picks is not None and rid not in picks:
                continue
            if r["state"] != "proposed":
                if picks is not None:
                    raise CadError(f"{r['number']}: {r['state']} already")
                continue
            key = picks.get(rid, "") if picks is not None else ""
            if key:
                if key not in {c["key"] for c in r["cands"]} and key != (r["h"] or {}).get("key"):
                    raise CadError(f"{r['number']}: {key} is not one offered for it")
                it = hy.index()["by_key"].get(key)
                down = it is not None and it["status"] in DOWN
                nxt = ("link" if down else "mark") if r["action"] == "oos" else ("back" if down else "")
            else:
                nxt = r["next"] if r["next"] in ("mark", "link", "back") else ""
                key = (r["h"] or {}).get("key", "")
            if not nxt or not key:
                if picks is not None:
                    raise CadError(f"{r['number']}: {'pick the hydrant' if r['next'] == 'confirm' else 'nothing to apply'}")
                continue
            todo.append((r, key, nxt))
        if not todo:
            return {"ok": True, "done": []}
        now = self.cad.clock()
        today = hy._today()
        done, wrote, freed = [], False, set()
        with self._lock:
            with self.store.tx() as db:
                idx = hy.index()
                for r, key, nxt in todo:
                    it = idx["by_key"].get(key)
                    if it is None:
                        continue
                    took = db.execute("UPDATE notif_hydrants SET state = ?, hydrant_key = ? WHERE id = ? AND state = 'proposed'",
                                      ("linked" if nxt in ("mark", "link") else "done", key, r["id"])).rowcount
                    if not took:                             # done meanwhile (another click, an auto-apply, a dismiss)
                        continue
                    if nxt == "mark":
                        wrote |= bool(hy._write(db, it, {"status": "oos", "status_reason": self._reason(inc)[:200],
                                                         "status_since": today, "status_until": None}, op, now))
                    if nxt in ("mark", "link"):
                        db.execute("UPDATE notif_hydrants SET state = 'linked', hydrant_key = ?, linked_at = ?, linked_by = ? "
                                   "WHERE id = ?", (key, now, op, r["id"]))
                    else:                                    # an "in" row: the hydrant in service, its links released
                        wrote |= bool(hy._write(db, it, {"status": "in_service", "status_reason": "", "status_until": None},
                                                op, now))
                        db.execute("UPDATE notif_hydrants SET state = 'done', hydrant_key = ?, back_at = ?, back_by = ? "
                                   "WHERE id = ?", (key, now, op, r["id"]))
                        freed |= self._release(db, key, op, now, f"{self._reason(inc)}")
                    done.append({"id": r["id"], "number": r["number"], "key": key, "did": nxt})
                said = {"mark": "Marked OOS", "link": "Linked (already OOS)", "back": "Back in service"}
                for what in ("mark", "link", "back"):
                    ns = [d["number"] for d in done if d["did"] == what]
                    if ns:
                        self.store._event(db, iid, "edit", f"Hydrants {', '.join(ns)}: {said[what]}", op, at=now)
        if wrote:
            hy._bump()
        self.settle(freed)
        return {"ok": True, "done": done}

    def _release(self, db, key: str, op: str, now: float, why: str) -> set[int]:
        """In the caller's transaction: hydrant `key` is back in service; every notification holding it lets it go (a line
        on each). -> those notifications."""
        got = set()
        it = self._hy().index()["by_key"].get(key) if self._hy() is not None else None
        no = (it or {}).get("number") or key
        for r in db.execute("SELECT id, incident_id FROM notif_hydrants WHERE hydrant_key = ? AND state = 'linked'",
                            (key,)).fetchall():
            db.execute("UPDATE notif_hydrants SET state = 'back', back_at = ?, back_by = ? WHERE id = ?", (now, op, r["id"]))
            self.store._event(db, r["incident_id"], "edit", f"Hydrant {no}: back in service" + (f" ({why})" if why else ""),
                              op, at=now)
            got.add(r["incident_id"])
        return got

    def back(self, iid: int, body: dict) -> dict:
        """{ids?}: these linked hydrants (None: all of them) back in service: the hydrant in service again, every
        notification holding it released."""
        hy = self._hy()
        if hy is None:
            raise CadError("no hydrant list here")
        op = str(body.get("operator") or "")[:320]
        ids = self._ids(body)
        rows = self.store.q("SELECT * FROM notif_hydrants WHERE incident_id = ? AND state = 'linked' ORDER BY id", (iid,))
        if ids is not None:
            have = {r["id"] for r in rows}
            if set(ids) - have:
                raise CadError("not linked to this notification")
            rows = [r for r in rows if r["id"] in ids]
        if not rows:
            raise CadError("no hydrant linked")
        now = self.cad.clock()
        wrote, freed = False, set()
        with self._lock:
            with self.store.tx() as db:
                idx = hy.index()
                for r in rows:
                    it = idx["by_key"].get(r["hydrant_key"])
                    if it is not None and it["status"] in DOWN:
                        wrote |= bool(hy._write(db, it, {"status": "in_service", "status_reason": "", "status_until": None},
                                                op, now))
                    freed |= self._release(db, r["hydrant_key"], op, now, "")
        if wrote:
            hy._bump()
        self.settle(freed)
        return {"ok": True, "back": len(rows)}

    def link(self, iid: int, body: dict) -> dict:
        """{key}: a hydrant already out of service (marked by hand from the call's list) held by this notification."""
        hy = self._hy()
        if hy is None:
            raise CadError("no hydrant list here")
        op = str(body.get("operator") or "")[:320]
        inc = self.store.incident(iid)
        if inc["status"] not in OPEN or not self.cad.lists.is_notification(inc["type"]):
            raise CadError("an open Notification's")
        key = str(body.get("key") or "")
        it = hy._item_or_404(key)
        if it["status"] not in DOWN:
            raise CadError(f"hydrant {it['number'] or key} is in service: mark it OOS first")
        now = self.cad.clock()
        no = it["number"] or key
        with self._lock:
            with self.store.tx() as db:
                rows = db.execute("SELECT id, number, state, hydrant_key FROM notif_hydrants WHERE incident_id = ? "
                                  "AND action = 'oos'", (iid,)).fetchall()
                if any(r["hydrant_key"] == key and r["state"] == "linked" for r in rows):
                    return {"ok": True}
                same = next((r for r in rows if num_key(r["number"]) == num_key(no) and r["state"] in ("proposed", "dismissed", "back")
                             and r["hydrant_key"] in ("", key)), None)
                if same is not None:
                    db.execute("UPDATE notif_hydrants SET state = 'linked', hydrant_key = ?, linked_at = ?, linked_by = ?, "
                               "back_at = NULL, back_by = '' WHERE id = ?", (key, now, op, same["id"]))
                else:
                    num = no if not any(num_key(r["number"]) == num_key(no) for r in rows) else f"{no} ({key})"
                    db.execute("INSERT INTO notif_hydrants (incident_id, number, action, hydrant_key, state, found_at, "
                               "linked_at, linked_by) VALUES (?, ?, 'oos', ?, 'linked', ?, ?, ?)", (iid, num, key, now, now, op))
                self.store._event(db, iid, "edit", f"Hydrant {no}: linked (OOS)", op, at=now)
        return {"ok": True}

    def dismiss(self, iid: int, body: dict) -> dict:
        """{id}: a row set aside (a number read wrongly; a link taken off: the hydrant itself unchanged)."""
        op = str(body.get("operator") or "")[:320]
        rid = body.get("id")
        if not isinstance(rid, int) or isinstance(rid, bool):
            raise CadError("id: a row id")
        r = self.store.one("SELECT * FROM notif_hydrants WHERE id = ? AND incident_id = ?", (rid, iid))
        if r is None:
            raise CadError("no such row")
        now = self.cad.clock()
        with self._lock:
            with self.store.tx() as db:
                took = db.execute("UPDATE notif_hydrants SET state = 'dismissed' WHERE id = ? AND state = ?",
                                  (rid, r["state"])).rowcount if r["state"] in ("proposed", "linked") else 0
                if not took:
                    raise CadError(f"{r['number']}: {r['state']} already")
                self.store._event(db, iid, "edit", f"Hydrant {r['number']}: "
                                  + ("unlinked" if r["state"] == "linked" else "set aside"), op, at=now)
        return {"ok": True}

    def on_status(self, key: str, old: str, new: str, op: str) -> None:
        """A hydrant's status changed on its card, in Settings or by CSV (sizeup/cad/hydrants.py): out of service -> not:
        every notification holding it lets it go. Never raises."""
        if old not in DOWN or new in DOWN:
            return
        try:
            now = self.cad.clock()
            with self._lock:
                with self.store.tx() as db:
                    freed = self._release(db, key, op, now, "")
            self.settle(freed)
        except Exception as e:  # noqa: BLE001 - the hydrant is saved
            from sizeup.fence import FencedWrite
            if isinstance(e, FencedWrite):
                raise
            self.cad.log(f"CAD: notification hydrants: {type(e).__name__}")

    def settle(self, iids) -> None:
        """Notifications whose last linked hydrant was just released: closed (auto_close), else they ask (the card)."""
        if not iids or not self.cfg()["auto_close"]:
            return
        codes = self.cad.lists.disposition_codes
        disp = "COMPLETE" if "COMPLETE" in codes else codes[0]
        for iid in sorted(iids):
            inc = self.store.incident(iid)
            if inc["status"] not in ("pending", "active") or self.held(iid):
                continue
            auto = getattr(self.cad, "auto", None)
            if (auto is not None and auto._manual(iid, "close")) or self.store.one(
                    "SELECT 1 AS x FROM assignments WHERE incident_id = ? AND cleared_at IS NULL LIMIT 1", (iid,)):
                continue                                     # reopened by a person, or a unit on it: theirs to close
            self.store.close(iid, disp, AUTO)
            self.cad.log(f"CAD: {inc['number'] or iid}: every hydrant back in service: closed")

    def close_check(self, iid: int, body: dict) -> None:
        """Cad.close, Cad.dismiss: a notification holding hydrants ends only when the person confirms (confirm: true). Its
        links stay (why those hydrants are out of service; their cards say it is closed)."""
        n = self.held(iid)
        if n and body.get("confirm") is not True:
            raise CadError(f"{_plural(n, 'hydrant')} still OOS on this notification: confirm to close")

    # -- Settings > Lists > Notification rules -----------------------------------------------------------------------------

    def settings_op(self, action: str, body: dict, op: str) -> dict:
        """set {rule} | delete {id} | move {id, dir} | auto_close {value} | test {text} (nothing written): admins,
        audited as Lists."""
        ed = self.cad.settings
        cur = ed.section(SECTION)
        rules = [dict(r) for r in (cur.get("rules") or []) if isinstance(r, dict)]
        kinds = {k["code"] for k in self.cad.lists.notification_kinds if not k.get("retired")}

        def page_out(out):
            return {**out, SECTION: page(ed.section(SECTION), self.cad.lists)}
        if action == "test":
            text = body.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX:
                raise CadError(f"text: 1 to {TEXT_MAX} characters")
            got = read(self.cfg(), [text])
            return {"kind": got["kind"], "kind_name": self._kind_name(got["kind"]), "rule": got["rule"],
                    "hydrants": [{"number": n, "action": a, "rule": r["name"]} for n, a, r in got["found"]]}
        if action == "auto_close":
            v = body.get("value")
            if not isinstance(v, bool):
                raise CadError("value: true or false")
            was = cur.get("auto_close") is True
            if v == was:
                return page_out({"ok": True})
            from sizeup.cad.settings import REMOVE
            return page_out(ed._commit([(SECTION, "auto_close", v if v else REMOVE)], op,
                                       [("lists", f"{SECTION}/auto_close", "on" if was else "off", "on" if v else "off")]))
        if action == "set":
            try:
                r = clean_rule(body.get("rule"), kinds)
            except ValueError as e:
                raise CadError(str(e)) from None
            i = next((n for n, x in enumerate(rules) if x.get("id") == r["id"]), None) if r["id"] else None
            if r["id"] and i is None:
                raise CadError(f"no rule {r['id']}")
            if i is None:
                if len(rules) >= MAX_RULES:
                    raise CadError(f"at most {MAX_RULES} rules")
                n = max([int(x["id"][1:]) for x in rules if re.fullmatch(r"r\d+", str(x.get("id") or ""))] or [0]) + 1
                r["id"] = f"r{n}"
                old = None
                rules.append(r)
            else:
                old = rules[i]
                if old == r:
                    return page_out({"ok": True})
                rules[i] = r
            return page_out(ed._commit([(SECTION, "rules", rules)], op, [("lists", f"{SECTION}/{r['id']}", old, r)]))
        rid = str(body.get("id") or "")
        i = next((n for n, x in enumerate(rules) if x.get("id") == rid), None)
        if i is None:
            raise CadError(f"no rule {rid!r}")
        if action == "delete":
            old = rules.pop(i)
            from sizeup.cad.settings import REMOVE
            return page_out(ed._commit([(SECTION, "rules", rules if rules else REMOVE)], op,
                                       [("lists", f"{SECTION}/{rid}", old, None)]))
        if action == "move":
            d = body.get("dir")
            if d not in (-1, 1) or isinstance(d, bool):
                raise CadError("dir: -1 (up) or 1 (down)")
            j = i + d
            if not 0 <= j < len(rules):
                return page_out({"ok": True})
            rules.insert(j, rules.pop(i))
            return page_out(ed._commit([(SECTION, "rules", rules)], op,
                                       [("lists", f"{SECTION}/_order", f"{rid} at {i + 1}", f"{rid} at {j + 1}")]))
        raise CadError("action: set, delete, move, auto_close or test")


def _who(op: str) -> str:
    return re.sub(r"\s*<[^<>]*>\s*$", "", op or "").strip()
