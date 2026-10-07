"""Settings: what the console's Settings page edits, kept in the CAD database over the data file.

data/cad_lists.json is the defaults. An edit is stored as an override (settings_overrides: section,
key -> JSON) and laid over the defaults (overlay()); the result is checked exactly like the file
(lists.build()) before anything is written, then used at once: the lists, the boards, the dispatch
wording, the tones and channels change for every open console on the next state push (no restart).
Reset to default = drop the override (one item, or a whole section).

    event_types, event_subtypes,   key CODE -> the item as edited ({"name", "color", "retired": true...});
    dispositions, received_via,              {"deleted": true}: a default item removed (only if never used)
    unit_statuses                  key "_order" -> [codes]: the list's order
    event_class                    key TYPE | TYPE/SUBTYPE | */SUBTYPE -> emergency | non-emergency | null (a default rule removed)
    geo_town_order                 key "_list" -> [towns]
    units                          key CODE -> the unit as edited ({kind, spoken, title, department, spare,
                                   response, retired}); key "_boards" -> the boards' layout
    dispatch_wording               key template name -> text
    dispatch                       key "tones" -> [[Hz, s], ...], "channels" -> [labels]

Codes never change (they are stored on events and exported): a rename changes the name only. An
item in use can't be deleted, only retired (named on old events, off the pick lists). Every change
writes settings_audit (who, page section, key, old -> new). operators: the people at the console
(a picker for the operator field; no sign-in yet). export() / import: every setting as one JSON
file, checked and shown as a diff (dry run) before it replaces anything.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path

from sizeup.cad import dispatcher, runcards
from sizeup.cad import lists as lists_mod
from sizeup.cad.lists import CLASSES, CODE, COLOR, LIST_SECTIONS, LONG_CODE, RESPONSES, ROLES, ListsError
from sizeup.cad.store import SCHEMA_VERSION, TZ, CadError

FORMAT, FORMAT_VERSION = "sizeup-cad-settings", 1
OPERATOR_ROLES = ("admin", "dispatcher", "viewer")
REMOVE = object()

# Where each stored section shows in Settings (the audit log's section).
PAGE_SECTION = {**{s: "lists" for s in LIST_SECTIONS}, "event_class": "lists", "geo_town_order": "lists",
                "units": "units", "dispatch_wording": "dispatch", "dispatch": "dispatch"}
AUDIT_SECTIONS = ("lists", "run cards", "units", "dispatch", "users", "system", "radios")
LIST_FIELDS = {"event_types": ("color",), "event_subtypes": (), "dispositions": (), "received_via": (),
               "unit_statuses": ("role", "bg", "fg")}
LIST_CODE = {"event_types": CODE, "event_subtypes": CODE, "dispositions": LONG_CODE, "received_via": LONG_CODE,
             "unit_statuses": LONG_CODE}
LIST_TITLES = {"event_types": "Event types", "event_subtypes": "Event subtypes", "dispositions": "Dispositions",
               "received_via": "Received via", "unit_statuses": "Unit statuses"}
WORDING = (("new", "First dispatch"), ("add_on", "Added to the assignment"), ("upgrade", "Upgrade"),
           ("full_response", "Full department response"), ("new_no_location", "First dispatch, no location"),
           ("already_group", "Add-on: units already there"), ("change_type", "Upgrade: new type"),
           ("change_type_full", "Upgrade: new type, full response"), ("change_full", "Upgrade: full response"),
           ("respond", "Upgrade: more units"))
MAX_TONES, MAX_GROUPS, MAX_CHANNELS = 8, 4, 8


def tone_groups(tones) -> list[list[list[float]]]:
    """[[hz, s], ...] (one group) or [[[hz, s], ...], ...] (one per department paged) -> groups."""
    return [[list(t) for t in g] for g in dispatcher.tone_groups(tones)]


def tones_value(groups: list) -> tuple:
    """What the dispatcher takes: one group flat (as the config writes it), several as groups."""
    return tuple(tuple(t) for t in groups[0]) if len(groups) == 1 else tuple(tuple(tuple(t) for t in g) for g in groups)


def _j(v) -> str:
    return "" if v is None else v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _norm(item: dict | None) -> dict:
    """An item without its code and empty parts, for 'is this the default?'."""
    return {k: v for k, v in (item or {}).items() if k != "code" and v not in ("", None, False)}


def _text(v, what: str, n: int, required: bool = True) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise CadError(f"{what}: text")
    v = v.strip()
    if required and not v:
        raise CadError(f"{what}: required")
    if len(v) > n:
        raise CadError(f"{what}: at most {n} characters")
    return v


def _dir(body: dict) -> int:
    d = body.get("dir")
    if d not in (-1, 1) or isinstance(d, bool):
        raise CadError("dir: -1 (up) or 1 (down)")
    return d


def overlay(d: dict, ov: dict) -> dict:
    """The data file's contents with the overrides laid over (unchecked: lists.build() checks it)."""
    d = copy.deepcopy(d)
    gone: dict[str, set] = {}
    for sec in LIST_SECTIONS:
        o = ov.get(sec) or {}
        if not o:
            continue
        items = [x if isinstance(x, dict) else {"code": x} for x in d.get(sec) or []]
        by = {str(x.get("code")): x for x in items}
        order = list(by)
        for code, v in o.items():
            if code.startswith("_") or not isinstance(v, dict):
                continue
            if v.get("deleted"):
                if code in by:
                    order.remove(code)
                    by.pop(code)
                    gone.setdefault(sec, set()).add(code)
                continue
            if code not in by:
                order.append(code)
            by[code] = {**v, "code": code}
        if isinstance(o.get("_order"), list):
            want = [c for c in o["_order"] if c in by]
            order = want + [c for c in order if c not in want]
        d[sec] = [by[c] for c in order]
    # A deleted type or subtype takes its rules, phrases and default cards with it.
    gt, gs = gone.get("event_types", set()), gone.get("event_subtypes", set())
    if gt or gs:
        def dead(k: str) -> bool:
            t, _, s = k.partition("/")
            return (t in gt) or (s in gs) or (t == "*" and s in gs)
        for m in ("event_class", "spoken"):
            d[m] = {k: v for k, v in (d.get(m) or {}).items() if not dead(k)}
        d["type_subtypes"] = {t: [s for s in v if s not in gs] for t, v in (d.get("type_subtypes") or {}).items() if t not in gt}
        d["run_cards"] = [c for c in d.get("run_cards") or [] if not dead(str(c.get("type", "")))]
    o = ov.get("event_class") or {}
    if o:
        ec = dict(d.get("event_class") or {})
        for k, v in o.items():
            if v is None:
                ec.pop(k, None)
            else:
                ec[k] = v
        d["event_class"] = ec
    o = ov.get("geo_town_order") or {}
    if isinstance(o.get("_list"), list):
        d["geo_town_order"] = list(o["_list"])
    o = ov.get("units") or {}
    if o:
        units = dict(d.get("units") or {})
        boards = copy.deepcopy(o["_boards"]) if isinstance(o.get("_boards"), list) else d.get("unit_boards") or []
        off = set()
        for code, v in o.items():
            if code.startswith("_") or not isinstance(v, dict):
                continue
            if v.get("deleted"):
                units.pop(code, None)
                off.add(code)
                continue
            units[code] = {k: x for k, x in v.items() if k != "board"}
            if v.get("retired"):
                off.add(code)
        for b in boards:
            b["units"] = [c for c in b.get("units") or [] if c not in off]
        d["units"], d["unit_boards"] = units, boards
        # The default cards (seed only) lose a slot naming a unit that is off the boards.
        on = {c for b in boards for c in b["units"] if c}
        cards = []
        for c in d.get("run_cards") or []:
            slots = [s for s in c.get("slots") or [] if not s.get("unit") or s["unit"] in on]
            if slots:
                cards.append(dict(c, slots=slots))
        d["run_cards"] = cards
    o = ov.get("dispatch_wording") or {}
    if o:
        d["dispatch_wording"] = dict(d.get("dispatch_wording") or {}, **o)
    return d


class Editor:
    """The Settings page's backend, for one Cad (sizeup/cad/service.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        self.path = Path(cad.s.lists) if cad.s.lists else lists_mod.DEFAULT_PATH
        self.defaults = lists_mod.read(self.path)
        self.base = cad.lists                                   # the data file alone
        self.config_tones = tone_groups(cad.s.tones)
        self.config_channels = tuple(cad.s.channels)

    # -- overrides ------------------------------------------------------------------------------------

    def overrides(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self.store.q("SELECT section, key, value FROM settings_overrides ORDER BY section, key"):
            out.setdefault(r["section"], {})[r["key"]] = json.loads(r["value"])
        return out

    def build(self, ov: dict) -> lists_mod.Lists:
        try:
            return lists_mod.build(overlay(self.defaults, ov), "settings", self.path)
        except ListsError as e:
            raise CadError(str(e).removeprefix("settings: ")) from None

    def startup(self) -> None:
        """At start: the saved edits over the data file. If they no longer fit it (the file changed
        under them), the file alone, said in the log: nothing is lost, Settings shows the edits."""
        ov = self.overrides()
        if not ov:
            return
        try:
            L = self.build(ov)
        except CadError as e:
            self.cad.log(f"CAD: settings edits not applied ({e}): using {self.path.name} as it is")
            return
        self._use(L, ov)

    def _use(self, L, ov: dict) -> None:
        self.cad.lists = self.store.lists = L
        d = ov.get("dispatch") or {}
        self.cad.s.tones = tones_value(d.get("tones", self.config_tones))
        self.cad.s.channels = tuple(d.get("channels", self.config_channels))
        self.cad._state = None

    def _commit(self, changes: list[tuple[str, str, object]], operator: str, audit: list[tuple[str, str, object, object]],
                check=None, extra_sql=None) -> dict:
        """Write overrides (value or REMOVE) after the whole result checks out; audit; apply now."""
        ov = self.overrides()
        trial = copy.deepcopy(ov)
        for sec, key, val in changes:
            if val is REMOVE:
                trial.get(sec, {}).pop(key, None)
            else:
                trial.setdefault(sec, {})[key] = val
        L = self.build(trial)
        if check:
            check(L)
        now = self.store.clock()
        with self.store.tx() as db:
            for sec, key, val in changes:
                if val is REMOVE:
                    db.execute("DELETE FROM settings_overrides WHERE section = ? AND key = ?", (sec, key))
                else:
                    db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) VALUES (?, ?, ?, ?, ?) "
                               "ON CONFLICT(section, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                               "operator = excluded.operator", (sec, key, json.dumps(val), now, operator))
            for section, key, old, new in audit:
                self._audit(db, now, operator, section, key, old, new)
            if extra_sql:
                extra_sql(db)
        self._use(L, trial)
        self.store.sync_units()          # new / retired / moved units, statuses: on the boards now
        self.store.rev += 1
        return {"ok": True}

    @staticmethod
    def _audit(db, at: float, operator: str, section: str, key: str, old, new) -> None:
        db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, ?, ?, ?, ?)",
                   (at, operator or "", section, key, _j(old), _j(new)))

    def audit_log(self, section: str = "", limit: int = 500) -> list[dict]:
        if section:
            return self.store.q("SELECT * FROM settings_audit WHERE section = ? ORDER BY at DESC, id DESC LIMIT ?",
                                (section, limit))
        return self.store.q("SELECT * FROM settings_audit ORDER BY at DESC, id DESC LIMIT ?", (limit,))

    def audit_card(self, operator: str, old: dict | None, new: dict | None) -> None:
        def show(c):
            return {"type": c["type"], "spoken": c["spoken"], "slots": c["slots"]} if c else None
        key = (new or old or {}).get("type", "")
        with self.store.tx() as db:
            if old and new and old["type"] != new["type"]:
                key = f"{old['type']} -> {new['type']}"
            self._audit(db, self.store.clock(), operator, "run cards", key, show(old), show(new))

    # -- what is used where ---------------------------------------------------------------------------

    def usage(self, sec: str) -> dict[str, int]:
        """code -> how many records use it (events, run cards, unit history): can't be deleted."""
        q, out = self.store.q, {}

        def add(rows):
            for r in rows:
                if r["k"]:
                    out[r["k"]] = out.get(r["k"], 0) + r["n"]
        if sec in ("event_types", "event_subtypes", "dispositions", "received_via"):
            col = {"event_types": "type", "event_subtypes": "subtype", "dispositions": "disposition",
                   "received_via": "received_via"}[sec]
            add(q(f"SELECT {col} AS k, COUNT(*) AS n FROM incidents GROUP BY {col}"))
            if sec in ("event_types", "event_subtypes"):
                for c in q("SELECT type FROM run_cards"):
                    t, _, s = c["type"].partition("/")
                    k = t if sec == "event_types" else s
                    if k:
                        out[k] = out.get(k, 0) + 1
        elif sec == "unit_statuses":
            add(q("SELECT status AS k, COUNT(*) AS n FROM units GROUP BY status"))
            add(q("SELECT old_status AS k, COUNT(*) AS n FROM unit_history GROUP BY old_status"))
            add(q("SELECT new_status AS k, COUNT(*) AS n FROM unit_history GROUP BY new_status"))
        elif sec == "units":
            add(q("SELECT u.name AS k, COUNT(*) AS n FROM assignments a JOIN units u ON u.id = a.unit_id GROUP BY u.name"))
            add(q("SELECT u.name AS k, COUNT(*) AS n FROM unit_history h JOIN units u ON u.id = h.unit_id GROUP BY u.name"))
            add(q("SELECT u.name AS k, COUNT(*) AS n FROM events e JOIN units u ON u.id = e.unit_id GROUP BY u.name"))
        return out

    def _used_why(self, sec: str, code: str) -> str:
        q = self.store.q
        parts = []
        if sec in ("event_types", "event_subtypes", "dispositions", "received_via"):
            col = {"event_types": "type", "event_subtypes": "subtype", "dispositions": "disposition",
                   "received_via": "received_via"}[sec]
            n = q(f"SELECT COUNT(*) AS n FROM incidents WHERE {col} = ?", (code,))[0]["n"]
            if n:
                parts.append(f"{n} event{'s' if n != 1 else ''}")
            if sec in ("event_types", "event_subtypes"):
                k = [c["type"] for c in q("SELECT type FROM run_cards")
                     if (c["type"].partition("/")[0] if sec == "event_types" else c["type"].partition("/")[2]) == code]
                if k:
                    parts.append(f"run card{'s' if len(k) != 1 else ''} {', '.join(k)}")
        elif self.usage(sec).get(code):
            parts.append(f"{self.usage(sec)[code]} record{'s' if self.usage(sec)[code] != 1 else ''}")
        return " and ".join(parts)

    def _cards_naming(self, code: str) -> list[str]:
        return sorted({c["type"] for c in runcards.cards(self.store) if any(s.get("unit") == code for s in c["slots"])})

    # -- the page ---------------------------------------------------------------------------------------

    def page(self) -> dict:
        L, ov = self.cad.lists, self.overrides()
        out: dict = {"rev": self.store.rev, "lists": {}, "titles": LIST_TITLES, "roles": list(ROLES),
                     "audit_sections": list(AUDIT_SECTIONS)}
        for sec in LIST_SECTIONS:
            base = {x["code"]: x for x in getattr(self.base, sec)}
            o, used = ov.get(sec, {}), self.usage(sec)
            rows = [dict(x, state="added" if x["code"] not in base else "edited" if x["code"] in o else "default",
                         used=used.get(x["code"], 0), default=base.get(x["code"])) for x in getattr(L, sec)]
            rows += [dict(base[c], state="deleted", used=0, default=base[c]) for c, v in o.items()
                     if isinstance(v, dict) and v.get("deleted") and c in base]
            out["lists"][sec] = {"items": rows, "ordered": "_order" in o, "edited": bool(o)}
        now_in = {}
        for u in self.store.units():
            now_in[u["status"]] = now_in.get(u["status"], 0) + 1
        for r in out["lists"]["unit_statuses"]["items"]:
            r["now"] = now_in.get(r["code"], 0)
        o = ov.get("event_class", {})
        out["event_class"] = {"rules": [{"key": k, "value": v, "state": "edited" if k in o else "default",
                                         "default": self.base.event_class.get(k)} for k, v in L.event_class.items()]
                              + [{"key": k, "value": None, "state": "deleted", "default": self.base.event_class.get(k)}
                                 for k, v in o.items() if v is None and k in self.base.event_class],
                              "edited": bool(o)}
        out["geo_town_order"] = {"towns": list(L.geo_town_order), "default": list(self.base.geo_town_order),
                                 "edited": "_list" in ov.get("geo_town_order", {})}
        out["units"] = self._unit_rows(L, ov)
        out["unit_kinds"] = [dict(k) for k in L.unit_kinds]
        out["boards"] = [{"id": b["id"], "title": b["title"]} for b in L.unit_boards]
        out["dispatch"] = self._dispatch_page(L, ov)
        out["operators"] = self.operators()
        out["system"] = self._system(ov)
        return out

    def _unit_rows(self, L, ov: dict) -> dict:
        o, used = ov.get("units", {}), self.usage("units")
        base_on = {c for c, _, _ in self.base.board_units()}
        live = {u["name"]: u for u in self.store.units()}
        rows, on = [], set()
        for b in L.unit_boards:
            pos = 0
            for code in b["units"]:
                if not code:
                    continue
                on.add(code)
                rows.append(self._unit_row(L, code, b["id"], pos, o, used, base_on, live))
                pos += 1
        for code, u in L.units.items():
            if code not in on and u.get("retired"):
                rows.append(self._unit_row(L, code, "", 0, o, used, base_on, live))
        for code, v in o.items():
            if isinstance(v, dict) and v.get("deleted"):
                rows.append(dict(self._unit_row(self.base, code, "", 0, {}, used, base_on, live), state="deleted"))
        return {"rows": rows, "edited": bool(o), "layout_edited": "_boards" in o}

    def _unit_row(self, L, code, board, pos, o, used, base_on, live) -> dict:
        u, info = L.units.get(code, {}), L.unit(code)
        lv = live.get(code) or {}
        return {"code": code, "board": board, "pos": pos, "spoken": u.get("spoken") or "", "title": info["title"],
                "kind": info["kind"], "department": info["department"], "spare": info["spare"], "response": info["response"],
                "retired": bool(u.get("retired")), "used": used.get(code, 0),
                "state": "added" if code not in base_on and code not in self.base.units else "edited" if code in o else "default",
                "status": lv.get("status", ""), "incident_number": lv.get("incident_number")}

    def _dispatch_page(self, L, ov: dict) -> dict:
        o, d = ov.get("dispatch_wording", {}), ov.get("dispatch", {})
        return {"wording": [{"key": k, "label": lab, "value": L.wording[k], "default": self.base.wording[k],
                             "state": "edited" if k in o else "default"} for k, lab in WORDING],
                "placeholders": ["department", "units", "location", "type", "type_bare", "already", "info", "change",
                                 "respond", "time", "full", "Full", "status"],
                "tones": {"value": tone_groups(self.cad.s.tones), "default": self.config_tones,
                          "edited": "tones" in d},
                "channels": {"value": list(self.cad.s.channels), "default": list(self.config_channels), "edited": "channels" in d},
                "voice": {"name": getattr(self.cad.s, "voice", ""), "rate_wpm": getattr(self.cad.s, "rate_wpm", 0),
                          "repeat": getattr(self.cad.s, "repeat", 0)},
                "transmit": {"enabled": self.cad.transmitter.enabled, "note": "Transmit is off: logged and played here only"},
                "department": self.cad.s.department}

    def _system(self, ov: dict) -> dict:
        try:
            raw = self.path.read_bytes()
        except OSError:
            raw = b""
        return {"data_file": lists_mod._shown_path(str(self.path)), "defaults_version": str(self.defaults.get("version") or ""),
                "sha256": hashlib.sha256(raw).hexdigest()[:12], "schema": SCHEMA_VERSION,
                "overrides": {k: len(v) for k, v in ov.items()}, "db": self.store.path}

    # -- edits ------------------------------------------------------------------------------------------

    def post(self, section: str, body: dict) -> dict:
        op = _text(body.get("operator"), "operator", 60, required=False)
        action = body.get("action")
        if not isinstance(action, str):
            raise CadError("action: required")
        if section in LIST_SECTIONS:
            return self._list_op(section, action, body, op)
        fn = {"event_class": self._class_op, "geo_town_order": self._towns_op, "units": self._units_op,
              "dispatch": self._dispatch_op, "users": self._users_op, "runcards": self._cards_op,
              "import": self._import_op}.get(section)
        if fn is None:
            raise CadError(f"no settings section {section!r}")
        return fn(action, body, op)

    def _clean_item(self, sec: str, raw, code: str) -> dict:
        if not isinstance(raw, dict):
            raise CadError("item: an object")
        out = {"name": _text(raw.get("name"), "name", 60)}
        for f in LIST_FIELDS[sec]:
            v = raw.get(f)
            if f == "role":
                if v not in (None, "") + ROLES:
                    raise CadError(f"role: one of {', '.join(ROLES)} (or none: committed)")
                if v:
                    out["role"] = v
            elif v not in (None, ""):
                if not isinstance(v, str) or not COLOR.fullmatch(v):
                    raise CadError(f"{f}: a color #rrggbb")
                out[f] = v.upper()
            elif f == "color" or (sec == "unit_statuses" and f == "bg"):
                raise CadError(f"{f}: required (#rrggbb)")
        return out

    def _list_op(self, sec: str, action: str, body: dict, op: str) -> dict:
        L, ov = self.cad.lists, self.overrides()
        o = ov.get(sec, {})
        items = {x["code"]: x for x in getattr(L, sec)}
        base = {x["code"]: x for x in getattr(self.base, sec)}
        title = LIST_TITLES[sec].lower()
        if action == "reset_section":
            gone = [c for c in o if not c.startswith("_") and c not in base and self.usage(sec).get(c)]
            if gone:
                raise CadError(f"can't reset {title}: {', '.join(gone)} (added here) are used; retire them instead")
            return self._commit([(sec, k, REMOVE) for k in o], op, [("lists", f"{sec}", f"{len(o)} edits", "reset to the defaults")])
        if action == "move":
            code = str(body.get("code") or "")
            order = list(items)
            if code not in order:
                raise CadError(f"no {code!r} in {title}")
            i = order.index(code)
            to = body.get("to")
            to = i + _dir(body) if to is None else to
            if not isinstance(to, int) or not 0 <= to < len(order):
                return {"ok": True}
            order.insert(to, order.pop(i))
            base_order = [c for c in base if c in items] + [c for c in order if c not in base]
            return self._commit([(sec, "_order", REMOVE if order == base_order else order)], op,
                                [("lists", f"{sec}/_order", f"{code} at {i + 1}", f"{code} at {to + 1}")])
        code = _text(body.get("code"), "code", 40).upper()
        if action == "add":
            if not LIST_CODE[sec].fullmatch(code):
                raise CadError(f"code: {code!r}: capital letters, digits and - _ (" + ("up to 24" if LIST_CODE[sec] is CODE else "up to 40") + ")")
            if code in items:
                raise CadError(f"{code} is already in {title}" + (" (retired: restore it)" if items[code].get("retired") else ""))
            item = self._clean_item(sec, body.get("item"), code)
            val = REMOVE if code in base and _norm(item) == _norm(base[code]) else item
            return self._commit([(sec, code, val)], op, [("lists", f"{sec}/{code}", None, item)])
        if code not in items and not (action == "reset" and code in o):
            raise CadError(f"no {code!r} in {title}")
        cur = items.get(code)
        if action == "edit":
            raw = body.get("item")
            if isinstance(raw, dict) and raw.get("code") not in (None, code):
                raise CadError("codes don't change (they are on events and in the exports): retire it and add a new one")
            item = self._clean_item(sec, dict(_norm(cur), **(raw or {})), code)
            if cur.get("retired"):
                item["retired"] = True
            val = REMOVE if code in base and _norm(item) == _norm(base[code]) else item
            return self._commit([(sec, code, val)], op, [("lists", f"{sec}/{code}", _norm(cur), item)])
        if action in ("retire", "restore"):
            on = action == "retire"
            if on and sec == "unit_statuses":
                n = sum(1 for u in self.store.units() if u["status"] == code)
                if n:
                    raise CadError(f"{n} unit{'s are' if n != 1 else ' is'} {cur['name']} now: change {'them' if n != 1 else 'it'} first")
            item = dict(_norm(cur), retired=True) if on else {k: v for k, v in _norm(cur).items() if k != "retired"}
            val = REMOVE if code in base and _norm(item) == _norm(base[code]) else item
            return self._commit([(sec, code, val)], op, [("lists", f"{sec}/{code}", "active" if on else "retired",
                                                          "retired" if on else "active")])
        if action == "delete":
            why = self._used_why(sec, code)
            if why:
                raise CadError(f"can't delete {cur['name']} ({code}): used by {why}. Retire it instead: "
                               "it stays on old events, off the pick lists")
            changes = [(sec, code, {"deleted": True} if code in base else REMOVE)]
            if isinstance(o.get("_order"), list) and code in o["_order"]:
                changes.append((sec, "_order", [c for c in o["_order"] if c != code]))
            return self._commit(changes, op, [("lists", f"{sec}/{code}", _norm(cur), "deleted")])
        if action == "reset":
            if code not in o:
                raise CadError(f"{code} is already the default")
            if code not in base:
                return self._list_op(sec, "delete", body, op)
            return self._commit([(sec, code, REMOVE)], op, [("lists", f"{sec}/{code}", o[code], "default")])
        raise CadError(f"action: add, edit, move, retire, restore, delete, reset or reset_section")

    def _class_op(self, action: str, body: dict, op: str) -> dict:
        o = self.overrides().get("event_class", {})
        cur, base = self.cad.lists.event_class, self.base.event_class
        if action == "reset_section":
            return self._commit([("event_class", k, REMOVE) for k in o], op, [("lists", "event_class", f"{len(o)} edits", "reset")])
        key = _text(body.get("key"), "key", 60).upper()
        if action == "set":
            v = body.get("value")
            if v not in CLASSES:
                raise CadError("value: emergency or non-emergency")
            return self._commit([("event_class", key, REMOVE if base.get(key) == v else v)], op,
                                [("lists", f"event_class/{key}", cur.get(key), v)])
        if action == "remove":
            if key not in cur:
                raise CadError(f"no rule for {key}")
            return self._commit([("event_class", key, None if key in base else REMOVE)], op,
                                [("lists", f"event_class/{key}", cur[key], None)])
        if action == "reset":
            if key not in o:
                raise CadError(f"{key} is already the default")
            return self._commit([("event_class", key, REMOVE)], op, [("lists", f"event_class/{key}", cur.get(key), base.get(key))])
        raise CadError("action: set, remove, reset or reset_section")

    def _towns_op(self, action: str, body: dict, op: str) -> dict:
        cur, base = list(self.cad.lists.geo_town_order), list(self.base.geo_town_order)
        if action == "reset_section":
            return self._commit([("geo_town_order", "_list", REMOVE)], op, [("lists", "geo_town_order", cur, base)])
        if action != "set":
            raise CadError("action: set or reset_section")
        towns = body.get("towns")
        if not isinstance(towns, list) or len(towns) > 40:
            raise CadError("towns: a list (up to 40)")
        towns = [_text(t, "town", 60) for t in towns]
        if len({t.lower() for t in towns}) != len(towns):
            raise CadError("towns: each town once")
        return self._commit([("geo_town_order", "_list", REMOVE if towns == base else towns)], op,
                            [("lists", "geo_town_order", cur, towns)])

    # -- units ------------------------------------------------------------------------------------------

    def _layout(self) -> list[dict]:
        return [{"id": b["id"], "title": b["title"], "units": list(b["units"])} for b in self.cad.lists.unit_boards]

    def _board_of(self, layout: list[dict], code: str) -> dict | None:
        return next((b for b in layout if code in b["units"]), None)

    def _layout_change(self, layout: list[dict]):
        base = [{"id": b["id"], "title": b["title"], "units": list(b["units"])} for b in self.base.unit_boards]
        return REMOVE if layout == base else layout

    def _clean_unit(self, raw, cur: dict | None = None) -> dict:
        """A unit as edited: {kind, spoken, title, department, spare, response} (missing: as it was)."""
        if not isinstance(raw, dict):
            raise CadError("unit: an object")
        cur, L = cur or {}, self.cad.lists
        g = lambda k: raw[k] if k in raw else cur.get(k)
        kind = g("kind") or "engine"
        if kind not in L.kind_ids:
            raise CadError(f"kind: one of {', '.join(L.kind_ids)}")
        out = {"kind": kind}
        for k, n in (("spoken", 40), ("title", 60), ("department", 40)):
            v = _text(g(k), k, n, required=False)
            if v:
                out[k] = v
        if g("spare"):
            out["spare"] = True
        r = raw.get("response") or lists_mod.unit_response(cur if cur else {"kind": kind}, L.attach_kinds)
        if r not in RESPONSES:
            raise CadError(f"response: one of {', '.join(RESPONSES)}")
        out["response"] = r
        return out

    def _same_unit(self, a: dict | None, b: dict | None, code: str) -> bool:
        """The same unit, however it is written (response given or from the flags, spoken = code)."""
        def k(u):
            u = u or {}
            return (u.get("kind", "agency"), (u.get("spoken") or code) if u.get("spoken") != code else code,
                    u.get("title") or "", u.get("department") or "", bool(u.get("spare")), bool(u.get("retired")),
                    lists_mod.unit_response(u, self.cad.lists.attach_kinds))
        return k(a) == k(b)

    def _units_op(self, action: str, body: dict, op: str) -> dict:
        L, ov = self.cad.lists, self.overrides()
        o = ov.get("units", {})
        layout = self._layout()
        live = {u["name"]: u for u in self.store.units()}
        if action == "reset_section":
            return self._commit([("units", k, REMOVE) for k in o], op, [("units", "units", f"{len(o)} edits", "reset to the defaults")])
        code = _text(body.get("code"), "code", 24).upper()
        on_board = self._board_of(layout, code)
        cur = L.units.get(code)
        base = self.base.units.get(code)
        base_known = base is not None or any(code in b["units"] for b in self.base.unit_boards)

        def def_change(new: dict):
            return REMOVE if base_known and self._same_unit(new, base, code) else new

        if action == "add":
            if not CODE.fullmatch(code):
                raise CadError(f"code: {code!r}: capital letters, digits and - _ (up to 24)")
            if on_board or cur is not None or code in live:
                raise CadError(f"{code} is already a unit" + (" (retired: restore it)" if cur and cur.get("retired") else ""))
            u = self._clean_unit(body.get("unit"))
            if not u.get("spoken"):
                u["spoken"] = code
            board = str((body.get("unit") or {}).get("board") or ("agencies" if u["kind"] in L.attach_kinds else "main"))
            b = next((x for x in layout if x["id"] == board), None)
            if b is None:
                raise CadError(f"board: one of {', '.join(x['id'] for x in layout)}")
            b["units"].append(code)
            return self._commit([("units", code, u), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, None, dict(u, board=board))])
        if cur is None and not on_board and not (action == "reset" and code in o):
            raise CadError(f"no unit {code!r}")
        cur = dict(cur or {})
        if action == "edit":
            raw = body.get("unit") or {}
            if isinstance(raw, dict) and raw.get("code") not in (None, code):
                raise CadError("unit codes don't change (they are on events and in the exports): retire it and add a new one")
            u = self._clean_unit(raw, cur)
            if cur.get("retired"):
                u["retired"] = True
            changes = [("units", code, def_change(u))]
            if u["response"] == "agency":
                cards = self._cards_naming(code)
                if cards:
                    raise CadError(f"{code} is named on run card{'s' if len(cards) > 1 else ''} {', '.join(cards)}: "
                                   "another agency's unit is never on a run card; take it off first")
            board = raw.get("board") if isinstance(raw, dict) else None
            if board and on_board and board != on_board["id"]:
                b = next((x for x in layout if x["id"] == board), None)
                if b is None:
                    raise CadError(f"board: one of {', '.join(x['id'] for x in layout)}")
                on_board["units"].remove(code)
                b["units"].append(code)
                changes.append(("units", "_boards", self._layout_change(layout)))
            return self._commit(changes, op, [("units", code, _norm(cur), dict(u, **({"board": board} if board else {})))])
        if action == "move":
            if not on_board:
                raise CadError(f"{code} is not on a board")
            us = on_board["units"]
            i = us.index(code)
            step = _dir(body)
            j = i + step
            while 0 <= j < len(us) and us[j] is None:
                j += step
            if not 0 <= j < len(us):
                return {"ok": True}
            us[i], us[j] = us[j], us[i]
            return self._commit([("units", "_boards", self._layout_change(layout))], op,
                                [("units", f"{code}/order", f"{i + 1}", f"{j + 1}")])
        if action == "retire":
            lv = live.get(code) or {}
            if lv.get("incident_id"):
                raise CadError(f"{code} is on {lv['incident_number']}: clear it first")
            cards = self._cards_naming(code)
            if cards:
                raise CadError(f"{code} is named on run card{'s' if len(cards) > 1 else ''} {', '.join(cards)}: take it off first")
            u = dict(_norm(cur), retired=True, board=on_board["id"] if on_board else "other")
            if on_board:
                on_board["units"].remove(code)
            return self._commit([("units", code, u), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, "on " + (on_board["id"] if on_board else "no board"), "retired")])
        if action == "restore":
            if not cur.get("retired"):
                raise CadError(f"{code} is not retired")
            raw_board = (o.get(code) or {}).get("board") or "main"
            b = next((x for x in layout if x["id"] == raw_board), layout[0])
            b["units"].append(code)
            u = {k: v for k, v in _norm(cur).items() if k not in ("retired", "board")}
            return self._commit([("units", code, def_change(u)), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, "retired", f"on {b['id']}")])
        if action == "delete":
            lv = live.get(code)
            n = self.usage("units").get(code, 0)
            if n:
                raise CadError(f"can't delete {code}: in {n} record{'s' if n != 1 else ''} (events, unit history). "
                               "Retire it instead: it stays on old events, off the boards")
            cards = self._cards_naming(code)
            if cards:
                raise CadError(f"{code} is named on run card{'s' if len(cards) > 1 else ''} {', '.join(cards)}: take it off first")
            if on_board:
                on_board["units"].remove(code)

            def drop_row(db):
                if lv:
                    db.execute("DELETE FROM units WHERE id = ?", (lv["id"],))
            return self._commit([("units", code, {"deleted": True} if base_known else REMOVE),
                                 ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, _norm(cur), "deleted")], extra_sql=drop_row)
        if action == "reset":
            if code not in o:
                raise CadError(f"{code} is already the default")
            if not base_known:
                return self._units_op("delete", body, op)
            changes = [("units", code, REMOVE)]
            if "_boards" in o:
                for b in layout:
                    if code in b["units"]:
                        b["units"].remove(code)
                home = next((b for b in self.base.unit_boards if code in b["units"]), None)
                if home:
                    tgt = next((b for b in layout if b["id"] == home["id"]), None)
                    if tgt is not None:
                        tgt["units"].insert(min(home["units"].index(code), len(tgt["units"])), code)
                changes.append(("units", "_boards", self._layout_change(layout)))
            return self._commit(changes, op, [("units", code, o[code], "default")])
        raise CadError("action: add, edit, move, retire, restore, delete, reset or reset_section")

    # -- dispatch -----------------------------------------------------------------------------------------

    def _tones(self, v) -> list[list[list[float]]]:
        """Tone groups (or one flat group), each [Hz, seconds]; saved as groups."""
        if not isinstance(v, list):
            raise CadError("tones: a list of [Hz, seconds], or of such groups")
        flat = all(isinstance(t, (list, tuple)) and len(t) == 2 and all(isinstance(x, (int, float)) for x in t) for t in v)
        groups = [v] if flat and v else v
        if len(groups) > MAX_GROUPS:
            raise CadError(f"tones: up to {MAX_GROUPS} groups (one per department paged)")
        out = []
        for gi, g in enumerate(groups):
            if not isinstance(g, list) or not 1 <= len(g) <= MAX_TONES:
                raise CadError(f"tones: group {gi + 1}: 1 to {MAX_TONES} tones")
            grp = []
            for i, t in enumerate(g):
                if not (isinstance(t, (list, tuple)) and len(t) == 2
                        and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in t)):
                    raise CadError(f"tones: group {gi + 1}, tone {i + 1}: [Hz, seconds]")
                hz, sec = float(t[0]), float(t[1])
                if not (50 <= hz <= 4000 and 0 < sec <= 10):
                    raise CadError(f"tones: group {gi + 1}, tone {i + 1}: 50 to 4000 Hz, for up to 10 s")
                grp.append([hz, sec])
            if sum(x for _, x in grp) > 20:
                raise CadError(f"tones: group {gi + 1}: 20 s in all at most")
            out.append(grp)
        return out

    def _channels(self, v) -> list[str]:
        if not isinstance(v, list) or not 1 <= len(v) <= MAX_CHANNELS:
            raise CadError(f"channels: 1 to {MAX_CHANNELS} labels")
        out = [_text(c, "channel", 40) for c in v]
        if len(set(out)) != len(out):
            raise CadError("channels: each label once")
        return out

    def _dispatch_op(self, action: str, body: dict, op: str) -> dict:
        ov = self.overrides()
        L = self.cad.lists
        if action == "preview":
            return self.preview(body.get("wording") or {})
        if action == "reset_section":
            ch = [("dispatch_wording", k, REMOVE) for k in ov.get("dispatch_wording", {})]
            ch += [("dispatch", k, REMOVE) for k in ov.get("dispatch", {})]
            return self._commit(ch, op, [("dispatch", "dispatch", f"{len(ch)} edits", "reset to the defaults")])
        key = _text(body.get("key"), "key", 40)
        if key in dict(WORDING):
            if action == "set":
                v = _text(body.get("value"), key, 400)
                return self._commit([("dispatch_wording", key, REMOVE if v == self.base.wording[key] else v)], op,
                                    [("dispatch", f"wording/{key}", L.wording[key], v)])
            if action == "reset":
                return self._commit([("dispatch_wording", key, REMOVE)], op,
                                    [("dispatch", f"wording/{key}", L.wording[key], self.base.wording[key])])
        elif key in ("tones", "channels"):
            cur = tone_groups(self.cad.s.tones) if key == "tones" else list(self.cad.s.channels)
            dflt = self.config_tones if key == "tones" else list(self.config_channels)
            if action == "set":
                v = self._tones(body.get("value")) if key == "tones" else self._channels(body.get("value"))
                return self._commit([("dispatch", key, REMOVE if v == dflt else v)], op, [("dispatch", key, cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", key, cur, dflt)])
        else:
            raise CadError(f"key: a template ({', '.join(k for k, _ in WORDING)}), tones or channels")
        raise CadError("action: set, reset, reset_section or preview")

    def preview(self, wording: dict) -> dict:
        """The dispatch wording on a sample event, as it would be said (templates not saved)."""
        if not isinstance(wording, dict):
            raise CadError("wording: an object")
        L = self.cad.lists
        w, errors = dict(L.wording), {}
        dummy = dict.fromkeys(("department", "units", "location", "type", "type_bare", "already", "info", "change",
                               "respond", "time", "full", "Full", "status"), "x")
        for k, v in wording.items():
            if k not in dict(WORDING) or not isinstance(v, str):
                continue
            try:
                v.format(**dummy)
            except (KeyError, IndexError, ValueError) as e:
                errors[k] = f"unknown or broken placeholder ({e})"
                continue
            if v.strip():
                w[k] = v
        inc = {"address": "209 Westchester Ave", "cross_street": "Pearl St"}
        at = self.store.clock()
        dep = self.cad.s.department
        alarm, stru = L.phrase("ALARM", "COMM"), L.phrase("STRU")
        c = lambda kind, **kw: dispatcher.compose(kind, department=dep, incident=inc, at=at, wording=w, **kw)
        try:
            out = {"new": c("new", units=["Engine 60", "Ladder 31", "Rescue 40", "23 98"], phrase=stru),
                   "add_on": c("add_on", units=["Engine 61"], phrase=alarm,
                               already=[("responding", ["Engine 60"]), ("on location", ["Ladder 31", "23 98"])]),
                   "upgrade": c("upgrade", units=[], phrase=alarm, info="smoke showing from the second floor",
                                new_phrase=stru, full=True),
                   "full": c("new", units=["Engine 60"], phrase=stru, full=True)}
        except (KeyError, IndexError, ValueError) as e:
            out = {}
            errors["_"] = f"can't render: {e}"
        return {"texts": out, "errors": errors,
                "sample": "Sample: 209 Westchester Ave, cross of Pearl St; a structure fire (first dispatch, full "
                          "response) and a commercial fire alarm (add-on, upgrade to a structure fire)."}

    # -- run cards: reset ------------------------------------------------------------------------------

    def _cards_op(self, action: str, body: dict, op: str) -> dict:
        if action == "reset":
            k = _text(body.get("type"), "type", 50)
            old = runcards.card(self.store, k)
            c = runcards.reset(self.store, k)
            self.audit_card(op, old, c)
            return c
        if action == "reset_section":
            n = len(runcards.cards(self.store))
            runcards.reset_all(self.store)
            with self.store.tx() as db:
                self._audit(db, self.store.clock(), op, "run cards", "run cards", f"{n} cards", "reset to the defaults")
            return {"ok": True}
        raise CadError("action: reset or reset_section")

    # -- users ----------------------------------------------------------------------------------------------

    def operators(self) -> list[dict]:
        rows = self.store.q("SELECT * FROM operators ORDER BY active DESC, name COLLATE NOCASE")
        for r in rows:
            r["active"] = bool(r["active"])
        return rows

    def operator_names(self) -> list[str]:
        return [r["name"] for r in self.store.q("SELECT name FROM operators WHERE active = 1 ORDER BY name COLLATE NOCASE")]

    def _users_op(self, action: str, body: dict, op: str) -> dict:
        now = self.store.clock()

        def role_of(v, dflt):
            v = v or dflt
            if v not in OPERATOR_ROLES:
                raise CadError(f"role: one of {', '.join(OPERATOR_ROLES)}")
            return v
        if action == "add":
            name = _text(body.get("name"), "name", 60)
            role = role_of(body.get("role"), "dispatcher")
            if self.store.one("SELECT 1 AS x FROM operators WHERE name = ?", (name,)):
                raise CadError(f"{name} is already an operator")
            with self.store.tx() as db:
                oid = db.execute("INSERT INTO operators (name, role, active, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                                 (name, role, now, now)).lastrowid
                self._audit(db, now, op, "users", name, None, {"role": role, "active": True})
            return self._operator(oid)
        oid = body.get("id")
        if not isinstance(oid, int) or isinstance(oid, bool):
            raise CadError("id: the operator's id")
        cur = self._operator(oid)
        new = dict(cur)
        if action == "edit":
            if "name" in body:
                new["name"] = _text(body.get("name"), "name", 60)
                if new["name"].lower() != cur["name"].lower() and self.store.one("SELECT 1 AS x FROM operators WHERE name = ?", (new["name"],)):
                    raise CadError(f"{new['name']} is already an operator")
            if "role" in body:
                new["role"] = role_of(body.get("role"), "")
            if "active" in body:
                if not isinstance(body["active"], bool):
                    raise CadError("active: true or false")
                new["active"] = body["active"]
        elif action in ("deactivate", "activate"):
            new["active"] = action == "activate"
        else:
            raise CadError("action: add, edit, deactivate or activate")
        diff = {k: (cur[k], new[k]) for k in ("name", "role", "active") if cur[k] != new[k]}
        if not diff:
            return cur
        with self.store.tx() as db:
            db.execute("UPDATE operators SET name = ?, role = ?, active = ?, updated_at = ? WHERE id = ?",
                       (new["name"], new["role"], int(new["active"]), now, oid))
            self._audit(db, now, op, "users", cur["name"], {k: a for k, (a, _) in diff.items()}, {k: b for k, (_, b) in diff.items()})
        return self._operator(oid)

    def _operator(self, oid: int) -> dict:
        r = self.store.one("SELECT * FROM operators WHERE id = ?", (oid,))
        if r is None:
            raise CadError(f"no operator {oid}")
        r["active"] = bool(r["active"])
        return r

    # -- export / import ------------------------------------------------------------------------------------

    def export(self) -> dict:
        return {"format": FORMAT, "version": FORMAT_VERSION,
                "exported_at": datetime.fromtimestamp(self.store.clock(), TZ).isoformat(timespec="seconds"),
                "data_file": lists_mod._shown_path(str(self.path)), "defaults_version": str(self.defaults.get("version") or ""),
                "overrides": self.overrides(),
                "run_cards": [{"type": c["type"], "spoken": c["spoken"], "is_default": c["is_default"], "slots": c["slots"]}
                              for c in runcards.cards(self.store)],
                "operators": [{"name": o["name"], "role": o["role"], "active": o["active"]} for o in self.operators()]}

    def _check_import(self, data) -> tuple[dict, list | None, list | None, object]:
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise CadError(f"not a settings file (format {FORMAT!r} expected)")
        if data.get("version") != FORMAT_VERSION:
            raise CadError(f"settings file version {data.get('version')!r}: this sizeup reads version {FORMAT_VERSION}")
        ov = data.get("overrides") or {}
        if not isinstance(ov, dict) or any(s not in PAGE_SECTION or not isinstance(v, dict) for s, v in ov.items()):
            raise CadError(f"overrides: sections from {', '.join(PAGE_SECTION)}")
        try:
            json.dumps(ov)
        except (TypeError, ValueError):
            raise CadError("overrides: plain JSON values") from None
        d = ov.get("dispatch") or {}
        if "tones" in d:
            self._tones(d["tones"])
        if "channels" in d:
            self._channels(d["channels"])
        if set(d) - {"tones", "channels"}:
            raise CadError("overrides.dispatch: tones and channels only")
        L = self.build(ov)
        # nothing in use may disappear
        for sec in LIST_SECTIONS:
            now = {x["code"] for x in getattr(self.cad.lists, sec)}
            then = {x["code"] for x in getattr(L, sec)}
            used = self.usage(sec)
            lost = sorted(c for c in now - then if used.get(c))
            if lost:
                raise CadError(f"{LIST_TITLES[sec]}: {', '.join(lost)} are used here and missing from the file (retire, don't delete)")
        on = {c for c, _, _ in L.board_units()}
        busy = sorted(u["name"] for u in self.store.units() if u["incident_id"] and u["name"] not in on)
        if busy:
            raise CadError(f"units on an event now would leave the boards: {', '.join(busy)}")
        cards = data.get("run_cards")
        if cards is not None:
            if not isinstance(cards, list) or len(cards) > 500:
                raise CadError("run_cards: a list")
            known = {c for c, _, _ in L.board_units()}
            seen = set()
            fire_kinds = set(L.kind_ids) - set(L.attach_kinds)
            for i, c in enumerate(cards):
                if not isinstance(c, dict):
                    raise CadError(f"run_cards[{i}]: an object")
                try:
                    t, s = L.check_key(str(c.get("type", "")))
                except ValueError as e:
                    raise CadError(f"run_cards[{i}]: {e}") from None
                k = lists_mod.key(t, s)
                if k in seen:
                    raise CadError(f"run_cards: {k} twice")
                seen.add(k)
                slots = c.get("slots")
                if not isinstance(slots, list) or not 1 <= len(slots) <= runcards.MAX_SLOTS:
                    raise CadError(f"run_cards[{i}].slots: 1 to {runcards.MAX_SLOTS}")
                for sl in slots:
                    if not isinstance(sl, dict):
                        raise CadError(f"run_cards[{i}]: a slot is an object")
                    if sl.get("unit"):
                        if sl["unit"] not in known or L.response(sl["unit"]) == "agency":
                            raise CadError(f"run_cards[{i}]: {sl['unit']!r} is not a fire unit on the boards")
                    elif not set(str(sl.get("kind", "")).split("|")) <= fire_kinds or not (
                            isinstance(sl.get("count", 1), int) and 1 <= sl.get("count", 1) <= runcards.MAX_COUNT):
                        raise CadError(f"run_cards[{i}]: kind {sl.get('kind')!r} x {sl.get('count')!r}: a fire apparatus kind, 1 to {runcards.MAX_COUNT}")
                c["type"] = k
        ops = data.get("operators")
        if ops is not None:
            if not isinstance(ops, list) or len(ops) > 500:
                raise CadError("operators: a list")
            names = set()
            for i, o in enumerate(ops):
                if not isinstance(o, dict):
                    raise CadError(f"operators[{i}]: an object")
                o["name"] = _text(o.get("name"), f"operators[{i}].name", 60)
                if o.get("role", "dispatcher") not in OPERATOR_ROLES or not isinstance(o.get("active", True), bool):
                    raise CadError(f"operators[{i}]: role one of {', '.join(OPERATOR_ROLES)}, active true/false")
                if o["name"].lower() in names:
                    raise CadError(f"operators: {o['name']} twice")
                names.add(o["name"].lower())
        return ov, cards, ops, L

    def _import_op(self, action: str, body: dict, op: str) -> dict:
        if action not in ("dry_run", "apply"):
            raise CadError("action: dry_run or apply")
        ov, cards, ops, L = self._check_import(body.get("settings"))
        cur = self.overrides()
        changes = []
        for sec in sorted(set(cur) | set(ov)):
            a, b = cur.get(sec, {}), ov.get(sec, {})
            for k in sorted(set(a) | set(b)):
                if a.get(k, REMOVE) != b.get(k, REMOVE):
                    changes.append({"section": PAGE_SECTION[sec], "store": sec, "key": k,
                                    "change": "added" if k not in a else "removed" if k not in b else "changed",
                                    "old": a.get(k), "new": b.get(k)})
        if cards is not None:
            have = {c["type"]: {"spoken": c["spoken"], "is_default": c["is_default"], "slots": c["slots"]}
                    for c in runcards.cards(self.store)}
            want = {c["type"]: {"spoken": str(c.get("spoken") or ""), "is_default": bool(c.get("is_default")),
                                "slots": [{"unit": s["unit"]} if s.get("unit") else {"kind": s["kind"], "count": int(s.get("count", 1))}
                                          for s in c["slots"]]} for c in cards}
            for k in sorted(set(have) | set(want)):
                if have.get(k) != want.get(k):
                    changes.append({"section": "run cards", "store": "run_cards", "key": k,
                                    "change": "added" if k not in have else "removed" if k not in want else "changed",
                                    "old": have.get(k), "new": want.get(k)})
        if ops is not None:
            have = {o["name"].lower(): o for o in self.operators()}
            for o in ops:
                h = have.get(o["name"].lower())
                new = {"role": o.get("role", "dispatcher"), "active": o.get("active", True)}
                if h is None or {"role": h["role"], "active": h["active"]} != new or h["name"] != o["name"]:
                    changes.append({"section": "users", "store": "operators", "key": o["name"],
                                    "change": "added" if h is None else "changed",
                                    "old": {"role": h["role"], "active": h["active"]} if h else None, "new": new})
        if action == "dry_run":
            return {"ok": True, "dry_run": True, "changes": changes,
                    "note": "Nothing changed yet. Operators not in the file are kept; run cards and edits are replaced."}
        now = self.store.clock()
        with self.store.tx() as db:
            db.execute("DELETE FROM settings_overrides")
            for sec, kv in ov.items():
                for k, v in kv.items():
                    db.execute("INSERT INTO settings_overrides (section, key, value, updated_at, operator) VALUES (?, ?, ?, ?, ?)",
                               (sec, k, json.dumps(v), now, op))
            if cards is not None:
                db.execute("DELETE FROM run_cards")
                for i, c in enumerate(cards):
                    cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort) VALUES (?, ?, ?, ?)",
                                     (c["type"], str(c.get("spoken") or "")[:120], int(bool(c.get("is_default"))), i)).lastrowid
                    runcards._write_slots(db, cid, c["slots"])
            if ops is not None:
                for o in ops:
                    row = db.execute("SELECT id FROM operators WHERE name = ?", (o["name"],)).fetchone()
                    if row:
                        db.execute("UPDATE operators SET name = ?, role = ?, active = ?, updated_at = ? WHERE id = ?",
                                   (o["name"], o.get("role", "dispatcher"), int(o.get("active", True)), now, row[0]))
                    else:
                        db.execute("INSERT INTO operators (name, role, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                                   (o["name"], o.get("role", "dispatcher"), int(o.get("active", True)), now, now))
            for c in changes:
                self._audit(db, now, op, c["section"], f"{c['store']}/{c['key']}" if c["store"] not in ("run_cards", "operators")
                            else c["key"], c["old"], c["new"])
            self._audit(db, now, op, "system", "import", None, f"{len(changes)} changes")
        self._use(L, ov)
        self.store.sync_units()
        self.store.rev += 1
        return {"ok": True, "dry_run": False, "changes": changes}
