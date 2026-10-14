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
    type_subtypes                  key TYPE -> [the subtypes it allows] ([]: no subtype)
    status_flow                    key STATUS | "*" -> [next statuses] (the incident's unit buttons) | null (removed)
    geo_town_order                 key "_list" -> [towns]
    units                          key CODE -> the unit as edited ({kind, spoken, title, department, spare,
                                   response, station, retired}); key "_boards" -> the boards' layout
    dispatch_wording               key template name -> text; "lead_in_joint_only" -> {town: text}
    dispatch_tones                 key joint town -> [[Hz, s], ...] | null (not set: PCFD's are played)
    dispatch                       key "tones" -> PCFD's tone groups, "channels" -> [labels], "voice", "speed";
                                   the ETA estimates (sizeup/cad/eta.py): "road_factor", "speed_mph", "turnout_min",
                                   "return_margin_min", "order" (nearest | card), "quarters_status"; "weather_speak"
                                   (true: an NWS warning is said once by the dispatch voice where a console plays
                                   it, never keyed; sizeup/weather.py, #132)
    channels                       key "FG1" | "South 2" | "tg:307" -> {record?, transcribe?, engine?}: which channel is
                                   kept, transcribed, and by what (sizeup/channels.py; Settings > Radios)
    stt                            key "vocabulary" -> [phrases] (Amazon Transcribe's custom vocabulary),
                                   "aws_monthly_limit_usd" -> the monthly AWS limit (sizeup/transcribe.py)
    tones                          the tone book (#80, #115, #122; sizeup/cad/tonelist.py): key "A/B" -> a pair named
                                   here {a_hz, b_hz, places, service, role?, confirmed?, page?} (#84/#115: {name, ...});
                                   "A/B@<name in the list>" -> an edit to a row of data/westchester_tones.csv {list,
                                   places, service, role?, confirmed?} or {list, deleted: true}; "entity:<places>|<service>"
                                   -> an entity {places, service, ours?, notes?, aliases?, merged_into?}. Read over the
                                   list, never written into it (sizeup/app.py _apply_named_tones)
    camera_wall                    key "margin_m" -> metres around our fire districts, "include" / "exclude" -> [camera
                                   ids]: the camera wall's area (#125, Settings > System; sizeup/roads.py Roads.area)

Codes never change (they are stored on events and exported): a rename changes the name only. An
item in use can't be deleted, only retired (named on old events, off the pick lists). Every change
writes settings_audit (who, page section, key, old -> new). operators: the people at the console
(a picker for the operator field), each optionally keyed by the email they sign in with through
Cloudflare Access on the hub (person(): the display name and callsign for a verified email; an email
not listed yet is added unnamed for an admin to name; standing(): what that person may do, enforced
by sizeup/cad/api.py). export() / import: every setting as one JSON file, checked and shown as a diff
(dry run) before it replaces anything.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from sizeup import channels as channels_mod
from sizeup.cad import dispatcher, eta, runcards, tonelist
from sizeup.cad import lists as lists_mod
from sizeup.cad.lists import CLASSES, CODE, COLOR, LIST_SECTIONS, LONG_CODE, RESPONSES, ROLES, ListsError
from sizeup.cad.store import OPERATOR_MAX, SCHEMA_VERSION, TZ, CadError
from sizeup import polly
from sizeup.radio.tones import pair_key
from sizeup.tts import KOKORO_VOICES

FORMAT, FORMAT_VERSION = "sizeup-cad-settings", 1
OPERATOR_ROLES = ("admin", "dispatcher", "viewer")
REMOVE = object()
EMAIL = re.compile(r"[^@\s<>]{1,64}@[^@\s<>]{1,190}\.[^@\s<>]{1,63}")

# Where each stored section shows in Settings (the audit log's section).
PAGE_SECTION = {**{s: "lists" for s in LIST_SECTIONS}, "event_class": "lists", "geo_town_order": "lists", "status_flow": "lists",
                "type_subtypes": "lists",
                "units": "units", "dispatch_wording": "dispatch", "dispatch_tones": "dispatch", "dispatch": "dispatch",
                "channels": "radios", "stt": "radios", "places": "lists", "tones": "radios"}
# The camera wall's area (#125, sizeup/roads.py Roads.area): Settings > System.
WALL_SECTION, WALL_KEYS, WALL_MARGIN = "camera_wall", ("margin_m", "include", "exclude"), (0, 5000, 1000)
PAGE_SECTION[WALL_SECTION] = "system"
WALL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,64}")      # sizeup/roads.py CAMERA_ID
MAX_WALL_IDS = 200
AUDIT_SECTIONS = ("lists", "run cards", "units", "dispatch", "users", "system", "radios")
LIST_FIELDS = {"event_types": ("color",), "event_subtypes": (), "dispositions": (), "received_via": (),
               "unit_statuses": ("role", "bg", "fg")}
LIST_CODE = {"event_types": CODE, "event_subtypes": CODE, "dispositions": LONG_CODE, "received_via": LONG_CODE,
             "unit_statuses": LONG_CODE}
LIST_TITLES = {"event_types": "Event types", "event_subtypes": "Event subtypes", "dispositions": "Dispositions",
               "received_via": "Received via", "unit_statuses": "Unit statuses"}
WORDING = (("new", "First dispatch"), ("add_on", "Added to the assignment"), ("upgrade", "Upgrade"),
           ("full_response", "Full department response"), ("remarks", "Dispatcher remarks"), ("lead_in", "Lead-in"),
           ("lead_in_joint", "Lead-in with a joint department"), ("new_no_location", "First dispatch, no location"),
           ("already_group", "Add-on: units already there"), ("change_type", "Upgrade: new type"),
           ("change_type_full", "Upgrade: new type, full response"), ("change_full", "Upgrade: full response"),
           ("respond", "Upgrade: more units"))
MAX_TONES, MAX_GROUPS, MAX_CHANNELS = 8, 4, 8
MAX_VOCAB, MAX_PHRASE_CHARS = 500, 100
MAX_PLACE_NAMES = 300
TONES_SECTION, MAX_NAMED_TONES, TONE_HZ = "tones", 400, tonelist.TONE_HZ     # the decoder's band (radio/tones.py)


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
            keep = {"help": by[code]["help"]} if code in by and by[code].get("help") and "help" not in v else {}   # the definition stays (#105)
            by[code] = {**keep, **v, "code": code}
        if isinstance(o.get("_order"), list):
            want = [c for c in o["_order"] if c in by]
            order = want + [c for c in order if c not in want]
        d[sec] = [by[c] for c in order]
    o = ov.get("type_subtypes") or {}
    if o:                                      # a type or subtype deleted since: its leftovers drop out
        def codes(sec):
            return {str(x.get("code") if isinstance(x, dict) else x) for x in d.get(sec) or []}
        types, subs = codes("event_types"), codes("event_subtypes")
        d["type_subtypes"] = dict(d.get("type_subtypes") or {}, **{t: [x for x in v if x in subs] for t, v in o.items()
                                                                     if t in types and isinstance(v, list)})
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
    o = ov.get("status_flow") or {}
    gone_st = gone.get("unit_statuses", set())
    if o or gone_st:
        fl = dict(d.get("status_flow") or {})
        for k, v in o.items():
            if v is None:
                fl.pop(k, None)
            else:
                fl[k] = v
        d["status_flow"] = {k: [c for c in v if c not in gone_st] for k, v in fl.items() if k not in gone_st}
    o = ov.get("geo_town_order") or {}
    if isinstance(o.get("_list"), list):
        d["geo_town_order"] = list(o["_list"])
    o = ov.get("units") or {}
    if o:
        units = dict(d.get("units") or {})
        boards = copy.deepcopy(o["_boards"]) if isinstance(o.get("_boards"), list) else d.get("unit_boards") or []
        meta = {b.get("id"): lists_mod.board_meta(b) for b in d.get("unit_boards") or [] if isinstance(b, dict)}
        for b in boards:                       # how a board shows follows the data file (a saved layout is its units)
            if isinstance(b, dict) and b.get("id") in meta:
                for k in lists_mod.BOARD_META:
                    b.pop(k, None)
                b.update(meta[b["id"]])
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
    o = ov.get("dispatch_tones") or {}
    if o:
        d["dispatch_tones"] = dict(d.get("dispatch_tones") or {}, **o)
    return d


class LastAdmin(CadError):
    """A change that would leave no active admin with a sign-in email (409: nobody could change
    Settings again). Only checked where roles are enforced (the hub with Cloudflare Access)."""


def body_places(body: dict) -> list[str]:
    """A request's places: places [names] or place (one)."""
    raw = body.get("places")
    if raw is None and body.get("place") not in (None, ""):
        raw = [body["place"]]
    if not isinstance(raw, list) or not raw:
        raise CadError("places: one or more place names")
    return tonelist._places({"places": raw}, "")


def _is_admin(o: dict) -> bool:
    return o.get("role") == "admin" and bool(o.get("active")) and bool(o.get("email"))


class Editor:
    """The Settings page's backend, for one Cad (sizeup/cad/service.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        self.path = Path(cad.s.lists) if cad.s.lists else lists_mod.DEFAULT_PATH
        self.defaults = lists_mod.read(self.path)
        self.base = cad.lists                                   # the data file alone
        self.config_tones = tone_groups(cad.s.tones)
        self.config_channels = tuple(cad.s.channels)
        self.config_voice = (getattr(cad.s, "voice", ""), getattr(cad.s, "speed", 1.0))
        # The tone list under the "tones" overrides (Settings > Radios > Tones, #115): the app sets it (use_tone_list):
        # the config's codes (data/westchester_tones.csv + [[tones]]), the names ours by config, unknown_tones.jsonl.
        self.base_tones: list = []
        self.base_ours: set[str] = set()
        self.unknown_log: Path | None = None
        self._heard_cache: tuple = ((), [])

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
        if hasattr(self.cad.s, "speed"):
            self.cad.s.voice = d.get("voice", self.config_voice[0])
            self.cad.s.speed = float(d.get("speed", self.config_voice[1]))
        self.cad.eta = eta.Params().with_overrides(d)
        self.cad.weather_speak = bool(d.get("weather_speak", False))     # sizeup/weather.py (#132): off by default
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
        o = ov.get("status_flow", {})
        out["status_flow"] = {"rows": [{"key": k, "next": list(v), "state": "edited" if k in o else "default",
                                        "default": list(self.base.status_flow.get(k, ())) or None}
                                       for k, v in L.status_flow.items()]
                              + [{"key": k, "next": [], "state": "deleted", "default": list(self.base.status_flow[k])}
                                 for k, v in o.items() if v is None and k in self.base.status_flow],
                              "edited": bool(o)}
        out["geo_town_order"] = {"towns": list(L.geo_town_order), "default": list(self.base.geo_town_order),
                                 "edited": "_list" in ov.get("geo_town_order", {})}
        out["place_names"] = {"items": self.local_places(), "max": MAX_PLACE_NAMES}
        out["units"] = self._unit_rows(L, ov)
        out["unit_kinds"] = [dict(k) for k in L.unit_kinds]
        out["boards"] = [{"id": b["id"], "title": b["title"], **lists_mod.board_meta(b)} for b in L.unit_boards]
        out["type_subtypes"] = self._map_page(L, ov)
        out["dispatch"] = self._dispatch_page(L, ov)
        out["operators"] = self.operators()
        out["system"] = self._system(ov)
        out["tones"] = self.tones_page()
        out["camera_wall"] = self.wall_page()
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
        return {"rows": rows, "edited": bool(o), "layout_edited": "_boards" in o,
                "stations": [f["name"] for f in getattr(self.cad, "firehouses", [])]}

    def _unit_row(self, L, code, board, pos, o, used, base_on, live) -> dict:
        u, info = L.units.get(code, {}), L.unit(code)
        lv = live.get(code) or {}
        return {"code": code, "board": board, "pos": pos, "spoken": u.get("spoken") or "", "title": info["title"],
                "kind": info["kind"], "department": info["department"], "spare": info["spare"], "response": info["response"],
                "station": L.station(code),
                "retired": bool(u.get("retired")), "used": used.get(code, 0), "named": info["named"],
                "state": "added" if code not in base_on and code not in self.base.units else "edited" if code in o else "default",
                "status": lv.get("status", ""), "incident_number": lv.get("incident_number")}

    def _dispatch_page(self, L, ov: dict) -> dict:
        o, d = ov.get("dispatch_wording", {}), ov.get("dispatch", {})
        return {"wording": [{"key": k, "label": lab, "value": L.wording[k], "default": self.base.wording[k],
                             "state": "edited" if k in o else "default"} for k, lab in WORDING],
                "placeholders": ["department", "units", "location", "type", "type_bare", "already", "info", "change",
                                 "respond", "remarks", "joint", "time", "full", "Full", "status"],
                "lead_in_joint_only": [{"town": t, "value": L.lead_in_joint_only.get(t, ""),
                                        "default": self.base.lead_in_joint_only.get(t, ""),
                                        "state": "edited" if "lead_in_joint_only" in o and
                                        L.lead_in_joint_only.get(t) != self.base.lead_in_joint_only.get(t) else "default"}
                                       for t in L.joint_response],
                "dept_tones": [{"town": t, "value": [list(x) for x in L.dispatch_tones[t]] if L.dispatch_tones.get(t) else None,
                                "default": [list(x) for x in self.base.dispatch_tones[t]] if self.base.dispatch_tones.get(t) else None,
                                "edited": t in ov.get("dispatch_tones", {})} for t in L.joint_response],
                "tones": {"value": tone_groups(self.cad.s.tones), "default": self.config_tones,
                          "edited": "tones" in d},
                "channels": {"value": list(self.cad.s.channels), "default": list(self.config_channels), "edited": "channels" in d},
                "voice": {"engine": getattr(self.cad.s, "tts_engine", "say"), "name": getattr(self.cad.s, "voice", ""),
                          "voices": self._voice_ids(), "default": self.config_voice[0], "default_speed": self.config_voice[1],
                          "edited": "voice" in d or "speed" in d,
                          "speed": getattr(self.cad.s, "speed", 1.0), "rate_wpm": getattr(self.cad.s, "rate_wpm", 0),
                          "fallback": getattr(self.cad.s, "fallback_voice", ""), "repeat": getattr(self.cad.s, "repeat", 0)},
                "transmit": {"enabled": self.cad.transmitter.enabled, "note": "Transmit is off: logged and played here only"},
                "weather_speak": {"value": bool(d.get("weather_speak", False)), "default": False, "edited": "weather_speak" in d},
                "department": self.cad.s.department,
                "eta": self._eta_page(L, d)}

    def _eta_page(self, L, d: dict) -> dict:
        """The recommendation's ETA estimates (sizeup/cad/eta.py): each value, its default, edited or not."""
        cur, dflt = self.cad.eta.as_dict(), eta.Params().as_dict()
        return {"numbers": [{"key": k, "label": lab, "unit": unit, "min": lo, "max": hi, "value": cur[k], "default": dflt[k],
                             "edited": k in d} for k, (_, lo, hi, lab, unit) in eta.NUMBERS.items()],
                "order": {"value": cur["order"], "default": dflt["order"], "edited": "order" in d, "choices": list(eta.ORDERS)},
                "quarters_status": {"value": cur["quarters_status"], "default": dflt["quarters_status"],
                                    "edited": "quarters_status" in d,
                                    "choices": [{"code": c, "name": L.name("unit_statuses", c)} for c in L.free]},
                "note": "Estimates only: straight-line distance × road factor at an average speed, plus turnout "
                        "from quarters. No road network or traffic. Shown as “~3 min est.”."}

    def _system(self, ov: dict) -> dict:
        try:
            raw = self.path.read_bytes()
        except OSError:
            raw = b""
        return {"data_file": lists_mod._shown_path(str(self.path)), "defaults_version": str(self.defaults.get("version") or ""),
                "sha256": hashlib.sha256(raw).hexdigest()[:12], "schema": SCHEMA_VERSION,
                "overrides": {k: len(v) for k, v in ov.items()}, "db": self.store.path}

    # -- edits ------------------------------------------------------------------------------------------

    def post(self, section: str, body: dict, guard: bool = False) -> dict:
        """guard: roles are enforced (the hub with Cloudflare Access): users and import may not take
        away the last active admin with a sign-in email (LastAdmin)."""
        op = _text(body.get("operator"), "operator", OPERATOR_MAX, required=False)
        action = body.get("action")
        if not isinstance(action, str):
            raise CadError("action: required")
        if section in LIST_SECTIONS:
            return self._list_op(section, action, body, op)
        fn = {"event_class": self._class_op, "status_flow": self._flow_op, "type_subtypes": self._map_op, "geo_town_order": self._towns_op, "units": self._units_op,
              "dispatch": self._dispatch_op, "users": self._users_op, "runcards": self._cards_op,
              "import": self._import_op, "channels": self._channels_op, "stt": self._stt_op,
              "places": self._places_op, TONES_SECTION: self._tones_op, WALL_SECTION: self._wall_op}.get(section)
        if fn is None:
            raise CadError(f"no settings section {section!r}")
        if section in ("users", "import"):
            return fn(action, body, op, guard)
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
            if sec == "unit_statuses":          # its next-step buttons go with it
                for k, v in ov.get("status_flow", {}).items():
                    if k == code:
                        changes.append(("status_flow", k, REMOVE))
                    elif isinstance(v, list) and code in v:
                        changes.append(("status_flow", k, [c for c in v if c != code] or REMOVE))
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

    def _flow_op(self, action: str, body: dict, op: str) -> dict:
        """The next-step buttons on a unit that is on an incident: status (or "*", any other) -> statuses."""
        o = self.overrides().get("status_flow", {})
        cur, base = self.cad.lists.status_flow, self.base.status_flow
        if action == "reset_section":
            return self._commit([("status_flow", k, REMOVE) for k in o], op, [("lists", "status_flow", f"{len(o)} edits", "reset")])
        key = _text(body.get("key"), "key", 40).upper()
        if action == "set":
            nxt = body.get("next")
            if not isinstance(nxt, list) or not all(isinstance(c, str) for c in nxt) or len(set(nxt)) != len(nxt):
                raise CadError("next: a list of unit status codes, each once")
            if not nxt:
                raise CadError("next: at least one status (remove the row to use \u201cany other\u201d)")
            same = key in base and list(base[key]) == nxt
            return self._commit([("status_flow", key, REMOVE if same else nxt)], op,
                                [("lists", f"status_flow/{key}", list(cur.get(key, ())) or None, nxt)])
        if action == "remove":
            if key not in cur:
                raise CadError(f"no next steps for {key}")
            if key == "*":
                raise CadError("\u201cany other status\u201d stays (edit its steps instead)")
            return self._commit([("status_flow", key, None if key in base else REMOVE)], op,
                                [("lists", f"status_flow/{key}", list(cur[key]), None)])
        if action == "reset":
            if key not in o:
                raise CadError(f"{key} is already the default")
            return self._commit([("status_flow", key, REMOVE)], op,
                                [("lists", f"status_flow/{key}", list(cur.get(key, ())) or None, list(base.get(key, ())) or None)])
        raise CadError("action: set, remove, reset or reset_section")

    def _map_page(self, L, ov: dict) -> dict:
        """The type -> subtype map, a row per type: what it allows, whether the department gave it or it
        is a proposal to confirm (until edited or confirmed here), the default."""
        o, tsp = ov.get("type_subtypes", {}), L.type_subtypes_partial
        rows = []
        for t in L.event_types:
            c = t["code"]
            row = {"type": c, "name": t["name"], "subtypes": list(L.type_subtypes.get(c, ())), "listed": c in L.type_subtypes,
                   "state": "edited" if c in o else "default", "default": list(self.base.type_subtypes.get(c, ())),
                   "source": "given" if c in tsp.get("given", ()) else "proposed"}
            if t.get("retired"):
                row["retired"] = True
            if c in tsp.get("confirm", {}) and c not in o:
                row["confirm"] = tsp["confirm"][c]
            rows.append(row)
        return {"rows": rows, "gaps": L.map_gaps(), "edited": bool(o)}

    def _map_op(self, action: str, body: dict, op: str) -> dict:
        """Which subtypes a type allows (a checkbox per subtype in Settings). set with the list as it is:
        confirmed (a proposal no longer marked "to confirm"). A subtype off the map stays on old events;
        a run card for that pair has to go first (it could never match an event again)."""
        L, o = self.cad.lists, self.overrides().get("type_subtypes", {})
        if action == "reset_section":
            return self._commit([("type_subtypes", k, REMOVE) for k in o], op, [("lists", "type_subtypes", f"{len(o)} edits", "reset")],
                                check=self._cards_fit)
        typ = _text(body.get("type"), "type", 24).upper()
        if typ not in {t["code"] for t in L.event_types}:
            raise CadError(f"no event type {typ!r}")
        cur = list(L.type_subtypes.get(typ, ()))
        if action == "set":
            subs = body.get("subtypes")
            if not isinstance(subs, list) or not all(isinstance(x, str) for x in subs) or len(set(subs)) != len(subs):
                raise CadError("subtypes: a list of subtype codes, each once")
            known = {x["code"] for x in L.event_subtypes}
            bad = [x for x in subs if x not in known]
            if bad:
                raise CadError(f"subtypes: {', '.join(bad)} not in the event subtypes")
            new_on = [x for x in subs if x not in cur and L.retired("event_subtypes", x)]
            if new_on:
                raise CadError(f"subtypes: {', '.join(new_on)} retired (restore it first)")
            order = [x["code"] for x in L.event_subtypes]
            subs = sorted(subs, key=lambda x: (x not in cur, cur.index(x) if x in cur else order.index(x)))
            same = typ in self.base.type_subtypes and set(self.base.type_subtypes[typ]) == set(subs) \
                and typ not in L.type_subtypes_partial.get("confirm", {})
            return self._commit([("type_subtypes", typ, REMOVE if same else subs)], op,
                                [("lists", f"type_subtypes/{typ}", cur if typ in L.type_subtypes else None, subs)],
                                check=self._cards_fit)
        if action == "reset":
            if typ not in o:
                raise CadError(f"{typ} is already the default")
            return self._commit([("type_subtypes", typ, REMOVE)], op,
                                [("lists", f"type_subtypes/{typ}", cur, list(self.base.type_subtypes.get(typ, ())))],
                                check=self._cards_fit)
        raise CadError("action: set, reset or reset_section")

    def _cards_fit(self, new) -> None:
        """A map edit can't leave a run card for a pair the map no longer allows (one already off it: kept)."""
        old = self.cad.lists
        lost = []
        for c in runcards.cards(self.store):
            t, _, sub = c["type"].partition("/")
            if sub and sub in old.subtypes_for(t) and sub not in new.subtypes_for(t):
                lost.append(c["type"])
        if lost:
            raise CadError(f"run card{'s' if len(lost) != 1 else ''} {', '.join(lost)} would no longer fit: "
                           "change or delete the card first (Run cards)")

    # -- the department's own names for places (#67): "the Waterfront", "Kohl's plaza" -------------------
    # Kept as overrides (section "places", key: the name in lower case) so they are audited, exported and
    # imported with the rest; the place search (Cad.search_places, the location box) offers them first.

    def local_places(self) -> list[dict]:
        rev = self.store.rev
        if getattr(self, "_places_cache", (None,))[0] != rev:
            rows = self.store.q("SELECT key, value FROM settings_overrides WHERE section = 'places' ORDER BY key")
            self._places_cache = (rev, [dict(json.loads(r["value"]), key=r["key"]) for r in rows])
        return [dict(r) for r in self._places_cache[1]]

    def _places_op(self, action: str, body: dict, op: str) -> dict:
        cur = {r["key"]: {k: v for k, v in r.items() if k != "key"} for r in self.local_places()}
        if action == "delete":
            key = _text(body.get("key"), "key", 80)
            if key not in cur:
                raise CadError(f"no place name {key!r}")
            return self._commit([("places", key, REMOVE)], op, [("lists", f"place/{cur[key]['name']}", cur[key], None)])
        if action != "set":
            raise CadError("action: set or delete")
        name = _text(body.get("name"), "name", 60)
        key = " ".join(re.sub(r"[^a-z0-9]+", " ", name.lower()).split())
        if not key:
            raise CadError("name: letters or digits")
        raw = body.get("aliases") or []
        if isinstance(raw, str):
            raw = [a for a in raw.split(",")]
        if not isinstance(raw, list) or len(raw) > 10:
            raise CadError("aliases: up to 10 other names")
        aliases = [a for a in (_text(a, "alias", 60, required=False) for a in raw) if a]
        where = body.get("place")
        if not isinstance(where, dict):
            raise CadError("place: pick where it is from the suggestions")
        label = _text(where.get("label"), "place", 200)
        try:
            lat, lon = float(where.get("lat")), float(where.get("lon"))
        except (TypeError, ValueError):
            raise CadError("place: lat and lon") from None
        if not (40.0 <= lat <= 42.0 and -75.0 <= lon <= -72.0):
            raise CadError("place: lat and lon in the area")
        if key not in cur and len(cur) >= MAX_PLACE_NAMES:
            raise CadError(f"at most {MAX_PLACE_NAMES} place names")
        val = {"name": name, "aliases": aliases, "label": label, "lat": round(lat, 6), "lon": round(lon, 6),
               "muni": _text(where.get("sub"), "town", 80, required=False),
               "note": _text(body.get("note"), "note", 120, required=False)}
        return self._commit([("places", key, val)], op, [("lists", f"place/{name}", cur.get(key), val)])

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
        return [{"id": b["id"], "title": b["title"], "units": list(b["units"]), **lists_mod.board_meta(b)}
                for b in self.cad.lists.unit_boards]

    def _board_of(self, layout: list[dict], code: str) -> dict | None:
        return next((b for b in layout if code in b["units"]), None)

    def _layout_change(self, layout: list[dict]):
        base = [{"id": b["id"], "title": b["title"], "units": list(b["units"]), **lists_mod.board_meta(b)}
                for b in self.base.unit_boards]
        return REMOVE if layout == base else layout

    def _default_board(self, kind: str, layout: list[dict]) -> str:
        """Where a unit goes with no board said: other agencies' kinds on "agencies", the rest on "main"."""
        want = "agencies" if kind in self.cad.lists.attach_kinds else "main"
        return want if any(b["id"] == want for b in layout) else layout[0]["id"]

    def _unit_code(self, raw: str, known) -> str:
        """The unit code as the department wrote it (242_eNG, 239_Cascd): exact, else the one that
        matches in any case; a code not known yet is upper case (a new unit)."""
        if raw in known:
            return raw
        return next((c for c in known if c and c.upper() == raw.upper()), raw.upper())

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
        st = _text(g("station"), "station", 80, required=False)      # home firehouse (the maps)
        if st:
            names = [f["name"] for f in getattr(self.cad, "firehouses", [])]
            if st not in names:
                raise CadError(f"station: one of {', '.join(names)} (or empty: not set)")
            out["station"] = st
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
                    u.get("station") or "",
                    lists_mod.unit_response(u, self.cad.lists.attach_kinds))
        return k(a) == k(b)

    def _units_op(self, action: str, body: dict, op: str) -> dict:
        L, ov = self.cad.lists, self.overrides()
        o = ov.get("units", {})
        layout = self._layout()
        live = {u["name"]: u for u in self.store.units()}
        if action == "reset_section":
            return self._commit([("units", k, REMOVE) for k in o], op, [("units", "units", f"{len(o)} edits", "reset to the defaults")])
        known = {c for b in layout for c in b["units"] if c} | set(L.units) | {k for k in o if not k.startswith("_")} | set(live)
        code = self._unit_code(_text(body.get("code"), "code", 24), known)
        on_board = self._board_of(layout, code)
        cur = L.units.get(code)
        base = self.base.units.get(code)
        base_known = base is not None or any(code in b["units"] for b in self.base.unit_boards)

        def def_change(new: dict):
            return REMOVE if base_known and self._same_unit(new, base, code) else new

        if action == "add":
            if on_board or cur is not None or code in live:
                raise CadError(f"{code} is already a unit" + (" (retired: restore it)" if cur and cur.get("retired") else ""))
            if not CODE.fullmatch(code):
                raise CadError(f"code: {code!r}: capital letters, digits and - _ (up to 24)")
            u = self._clean_unit(body.get("unit"))
            if not u.get("spoken"):
                u["spoken"] = code
            board = str((body.get("unit") or {}).get("board") or self._default_board(u["kind"], layout))
            b = next((x for x in layout if x["id"] == board), None)
            if b is None:
                raise CadError(f"board: one of {', '.join(x['id'] for x in layout)}")
            b["units"].append(code)
            return self._commit([("units", code, u), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, None, dict(u, board=board))])
        if cur is None and not on_board and not (action == "reset" and code in o):
            raise CadError(f"no unit {code!r}")
        cur = dict(cur or {})
        if action == "station":                         # home firehouse only (the maps' station picker)
            old = L.station(code)
            u = self._clean_unit({"station": body.get("station")}, cur)
            if u.get("station", "") == old:
                return {"ok": True}
            if cur.get("retired"):
                u["retired"] = True
            return self._commit([("units", code, def_change(u))], op, [("units", f"{code}/station", old, u.get("station", ""))])
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
            u = dict(_norm(cur), retired=True, board=on_board["id"] if on_board else self._default_board(cur.get("kind", "agency"), layout))
            if on_board:
                on_board["units"].remove(code)
            return self._commit([("units", code, u), ("units", "_boards", self._layout_change(layout))], op,
                                [("units", code, "on " + (on_board["id"] if on_board else "no board"), "retired")])
        if action == "restore":
            if not cur.get("retired"):
                raise CadError(f"{code} is not retired")
            raw_board = (o.get(code) or {}).get("board") or self._default_board(cur.get("kind", "agency"), layout)
            b = next((x for x in layout if x["id"] == raw_board), None) or next(
                x for x in layout if x["id"] == self._default_board(cur.get("kind", "agency"), layout))
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
        if action == "voice_preview":
            return self.cad.voice_preview()
        if action == "reset_section":
            ch = [("dispatch_wording", k, REMOVE) for k in ov.get("dispatch_wording", {})]
            ch += [("dispatch_tones", k, REMOVE) for k in ov.get("dispatch_tones", {})]
            ch += [("dispatch", k, REMOVE) for k in ov.get("dispatch", {})]
            return self._commit(ch, op, [("dispatch", "dispatch", f"{len(ch)} edits", "reset to the defaults")])
        key = _text(body.get("key"), "key", 80)
        if key.startswith("lead_in_joint_only/") or key.startswith("tones/"):
            what, _, town = key.partition("/")
            if town not in L.joint_response:
                raise CadError(f"{town!r} is not a joint-response department")
            if what == "tones":
                cur = [list(x) for x in L.dispatch_tones[town]] if L.dispatch_tones.get(town) else None
                if action == "set":
                    v = body.get("value")
                    v = None if v is None else self._tones(v)
                    if v is not None and len(v) != 1:
                        raise CadError("tones: one group for a department")
                    v = v[0] if v else None
                    base = [list(x) for x in self.base.dispatch_tones[town]] if self.base.dispatch_tones.get(town) else None
                    return self._commit([("dispatch_tones", town, REMOVE if v == base else v)], op,
                                        [("dispatch", f"tones/{town}", cur, v)])
                if action == "reset":
                    return self._commit([("dispatch_tones", town, REMOVE)], op, [("dispatch", f"tones/{town}", cur, "default")])
            else:
                only = dict(L.lead_in_joint_only)
                if action == "set":
                    only[town] = _text(body.get("value"), "lead-in", 80)
                elif action == "reset":
                    only[town] = self.base.lead_in_joint_only.get(town, "")
                    only = {t: v for t, v in only.items() if v}
                else:
                    raise CadError("action: set or reset")
                same = only == dict(self.base.lead_in_joint_only)
                return self._commit([("dispatch_wording", "lead_in_joint_only", REMOVE if same else only)], op,
                                    [("dispatch", key, L.lead_in_joint_only.get(town), only.get(town))])
        elif key in ("voice", "speed"):
            dflt = self.config_voice[0 if key == "voice" else 1]
            cur = getattr(self.cad.s, key, dflt)
            if action == "set":
                v = body.get("value")
                if key == "speed":
                    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.5 <= v <= 2.0:
                        raise CadError("speed: 0.5 to 2.0")
                    v = round(float(v), 2)
                elif getattr(self.cad.s, "tts_engine", "say") in ("kokoro", "polly"):
                    if v not in self._voice_ids():
                        raise CadError(f"voice: one of {', '.join(self._voice_ids()[:4])}, ...")
                else:
                    v = _text(v, "voice", 60)
                return self._commit([("dispatch", key, REMOVE if v == dflt else v)], op, [("dispatch", key, cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", key, cur, dflt)])
        elif key in dict(WORDING):
            if action == "set":
                v = _text(body.get("value"), key, 400)
                return self._commit([("dispatch_wording", key, REMOVE if v == self.base.wording[key] else v)], op,
                                    [("dispatch", f"wording/{key}", L.wording[key], v)])
            if action == "reset":
                return self._commit([("dispatch_wording", key, REMOVE)], op,
                                    [("dispatch", f"wording/{key}", L.wording[key], self.base.wording[key])])
        elif key in eta.KEYS:
            cur, dflt = self.cad.eta.as_dict()[key], eta.Params().as_dict()[key]
            if action == "set":
                try:
                    v = eta.check(key, body.get("value"), L.free)
                except ValueError as e:
                    raise CadError(str(e)) from None
                return self._commit([("dispatch", key, REMOVE if v == dflt else v)], op, [("dispatch", f"eta/{key}", cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", f"eta/{key}", cur, dflt)])
        elif key == "weather_speak":                  # an NWS warning said once by the dispatch voice (#132)
            cur = bool((ov.get("dispatch") or {}).get("weather_speak", False))
            if action == "set":
                v = body.get("value")
                if not isinstance(v, bool):
                    raise CadError("weather_speak: true or false")
                return self._commit([("dispatch", key, v if v else REMOVE)], op, [("dispatch", key, cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", key, cur, False)])
        elif key in ("tones", "channels"):
            cur = tone_groups(self.cad.s.tones) if key == "tones" else list(self.cad.s.channels)
            dflt = self.config_tones if key == "tones" else list(self.config_channels)
            if action == "set":
                v = self._tones(body.get("value")) if key == "tones" else self._channels(body.get("value"))
                return self._commit([("dispatch", key, REMOVE if v == dflt else v)], op, [("dispatch", key, cur, v)])
            if action == "reset":
                return self._commit([("dispatch", key, REMOVE)], op, [("dispatch", key, cur, dflt)])
        else:
            raise CadError(f"key: a template ({', '.join(k for k, _ in WORDING)}), tones, tones/<town>, "
                           f"lead_in_joint_only/<town>, channels, voice, speed, weather_speak or {', '.join(eta.KEYS)}")
        raise CadError("action: set, reset, reset_section, preview or voice_preview")

    def _voice_ids(self) -> list[str]:
        """The voices the engine set in the config offers (Polly's: men first)."""
        engine = getattr(self.cad.s, "tts_engine", "say")
        return list(polly.VOICES) if engine == "polly" else list(KOKORO_VOICES) if engine == "kokoro" else []

    def preview(self, wording: dict) -> dict:
        """The dispatch wording on a sample event, as it would be said (templates not saved)."""
        if not isinstance(wording, dict):
            raise CadError("wording: an object")
        L = self.cad.lists
        w, errors = dict(L.wording), {}
        dummy = dict.fromkeys(("department", "units", "location", "type", "type_bare", "already", "info", "change",
                               "respond", "time", "full", "Full", "status", "remarks", "joint"), "x")
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
        only = dict(L.lead_in_joint_only)
        if isinstance(wording.get("lead_in_joint_only"), dict):
            only.update({k: v for k, v in wording["lead_in_joint_only"].items() if isinstance(v, str) and v.strip()})
        joint = next(iter(L.joint_response), "")

        def c(kind, lead=None, **kw):
            lead = lead or dispatcher.lead_in(dep, [], True, False, w, only)
            return dispatcher.compose(kind, department=lead, incident=inc, at=at, wording=w, **kw)
        try:
            out = {"new": c("new", units=["Engine 60", "Ladder 31", "Rescue 40", "23 98"], phrase=stru,
                            remarks="smoke showing from the second floor"),
                   "add_on": c("add_on", units=["Engine 61"], phrase=alarm,
                               already=[("responding", ["Engine 60"]), ("on location", ["Ladder 31", "23 98"])]),
                   "upgrade": c("upgrade", units=[], phrase=alarm, info="smoke showing from the second floor",
                                new_phrase=stru, full=True),
                   "full": c("new", units=["Engine 60"], phrase=stru, full=True)}
            if joint:
                out["joint"] = c("new", lead=dispatcher.lead_in(dep, [joint], True, False, w, only),
                                 units=["Engine 60", "Engine 14", "Ladder 30"], phrase=stru)
                out["joint_only"] = c("new", lead=dispatcher.lead_in(dep, [joint], False, False, w, only),
                                      units=["Engine 14"], phrase=alarm)
        except (KeyError, IndexError, ValueError) as e:
            out = {}
            errors["_"] = f"can't render: {e}"
        return {"texts": out, "errors": errors,
                "sample": "Sample: 209 Westchester Ave, cross of Pearl St; a structure fire (first dispatch with remarks, "
                          "full response, with a joint department) and a commercial fire alarm (add-on, upgrade, only the "
                          "joint department's units)."}

    # -- radios: channels (record / transcribe / engine) and speech-to-text (sizeup/channels.py) ---------------

    def channel_overrides(self) -> dict:
        return dict(self.overrides().get(channels_mod.SECTION) or {})

    def _channel_value(self, key: str, cur: dict, vals: dict):
        """The override to store for key: only what differs from the default (REMOVE: nothing does)."""
        dflt = channels_mod.defaults(key, self._aws_channels())
        new = {k: v for k, v in {**cur, **vals}.items() if k in channels_mod.FIELDS and v != dflt.get(k)}
        if channels_mod.is_tg(key):
            new.pop("record", None)
        return new or REMOVE

    def _aws_channels(self) -> tuple:
        ctx = self.cad.radio_ctx
        return tuple(ctx.aws_channels()) if ctx is not None else channels_mod.DEFAULT_AWS

    def _channels_op(self, action: str, body: dict, op: str) -> dict:
        """set {key, record?, transcribe?, engine?} | set_many {keys, transcribe?, engine?} | reset {key}.
        A talkgroup's record goes to its own Record switch (the station's TalkgroupPrefs)."""
        ctx = self.cad.radio_ctx
        locked = ctx.channels_locked() if ctx is not None else ""
        if locked:
            raise CadError(locked)
        ov = self.channel_overrides()
        aws = self._aws_channels()
        if action in ("set", "set_many"):
            keys = [body.get("key")] if action == "set" else body.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 2000:
                raise CadError("keys: a list of channels")
            try:
                vals = {k: channels_mod.clean(k, {f: body.get(f) for f in channels_mod.FIELDS}) for k in keys}
            except ValueError as e:
                raise CadError(str(e)) from None
            changes, audit = [], []
            for key, v in vals.items():
                if channels_mod.is_tg(key) and "record" in v:
                    rec = v.pop("record")
                    err = ctx.set_talkgroup(int(key[3:]), rec) if ctx is not None else "no talkgroups here"
                    if err:
                        raise CadError(err)
                if not v:
                    continue
                cur = ov.get(key) or {}
                new = self._channel_value(key, cur, v)
                before, after = channels_mod.pref(key, ov, aws), channels_mod.pref(key, {key: {} if new is REMOVE else new}, aws)
                if before != after:
                    changes.append((channels_mod.SECTION, key, new))
                    audit.append(("radios", f"channel/{key}", {k: before[k] for k in v}, {k: after[k] for k in v}))
            if not changes:
                return {"ok": True}
            out = self._commit(changes, op, audit)
        elif action == "reset":
            key = _text(body.get("key"), "key", 40)
            if key not in ov:
                return {"ok": True}
            out = self._commit([(channels_mod.SECTION, key, REMOVE)], op,
                               [("radios", f"channel/{key}", ov[key], "default")])
        else:
            raise CadError("action: set, set_many or reset")
        if ctx is not None:
            ctx.channels_changed()
        return out

    def replace_channels(self, prefs: dict, operator: str) -> bool:
        """The whole channel map as the hub sent it (the station applying the CAD of record's choices).
        True when anything changed."""
        cur = self.channel_overrides()
        changes = [(channels_mod.SECTION, k, REMOVE) for k in cur if k not in prefs]
        changes += [(channels_mod.SECTION, k, v) for k, v in prefs.items() if cur.get(k) != v]
        if not changes:
            return False
        self._commit(changes, operator, [("radios", f"channel/{k}", cur.get(k), None if v is REMOVE else v)
                                         for _s, k, v in changes])
        return True

    # -- tone pairs named from the console (#80) --------------------------------------------------------
    # Kept as overrides (section "tones", key "A/B" in Hz) so they are audited, exported and imported with
    # the rest, and sent to the station with the channel choices (the hub's set_channel_prefs job).

    def tone_overrides(self) -> dict:
        return dict(self.overrides().get(TONES_SECTION) or {})

    def tone_rows(self) -> list[dict]:
        """GET /api/cad/tones: each pair named here (not the edits to list rows or entities), who named it and when."""
        rows = self.store.q("SELECT key, value, updated_at, operator FROM settings_overrides WHERE section = ? ORDER BY key",
                            (TONES_SECTION,))
        return [dict(json.loads(r["value"]), key=r["key"], at=r["updated_at"], operator=r["operator"] or "") for r in rows
                if not tonelist.is_list_key(r["key"]) and not r["key"].startswith(tonelist.ENTITY)]

    @staticmethod
    def _clean_tone(v, where: str = "") -> tuple[str, dict]:
        """A pair {a_hz, b_hz, places, service, role?, confirmed?, page?, list?} (or #84's / #115's {name, ...}), a list
        row taken out {list, a_hz, b_hz, deleted}, or an entity {places, service, ours?, notes?, aliases?, merged_into?}
        checked -> (key, the value stored). CadError. sizeup/cad/tonelist.py clean()."""
        return tonelist.clean(v, where)

    def use_tone_list(self, codes, ours, unknown_log: Path | None = None) -> None:
        self.base_tones, self.base_ours, self.unknown_log = list(codes), set(ours), unknown_log

    def _locked(self) -> set[str]:
        """The names that are ours whatever is set here: PCFD's and Rye Brook's (the config's ours)."""
        return set(self.base_ours) | set(self.cad.s.monitor_departments)

    def _heard(self) -> list:
        """Every pair heard (tonelist.heard), kept until a page row or unknown_tones.jsonl changes."""
        r = self.store.q("SELECT COUNT(*) AS n, MAX(updated_at) AS u FROM radio_traffic WHERE tones != ''")[0]
        try:
            st = self.unknown_log.stat() if self.unknown_log else None
            log = (st.st_size, st.st_mtime) if st else None
        except OSError:
            log = None
        sig = (r["n"], r["u"], log)
        if self._heard_cache[0] != sig:
            rows = self.store.q("SELECT id, start, channel, tones, voice_at FROM radio_traffic WHERE tones != ''")
            self._heard_cache = (sig, tonelist.heard(rows, self.unknown_log))
        return self._heard_cache[1]

    def tone_book(self, ov: dict | None = None) -> tonelist.Book:
        """The tone book (#122): the list read as entities (places + service) with their pairs, the overrides on it."""
        return tonelist.Book(self.base_tones, self.tone_overrides() if ov is None else ov, self._locked())

    def tone_plan(self, ov: dict | None = None):
        from sizeup.radio.tones import TonePlan
        return TonePlan(self.tone_book(ov).codes())

    def tone_choices(self, book: tonelist.Book | None = None) -> dict:
        """What the dialog and the unnamed list pick from: the entities (and the county's agencies), the places, the
        services."""
        book = book or self.tone_book()
        return {"choices": tonelist.entity_choices(book), "places": tonelist.places(book),
                "services": [[s, tonelist.LABEL[s]] for s in tonelist.SERVICES]}

    def tones_page(self) -> dict:
        """Settings > Radios > Tones: every entity with its pairs, the pairs heard that nobody's code matches, what the
        list's reading did (report)."""
        ov = self.tone_overrides()
        meta = {r["key"]: {"operator": r["operator"] or "", "at": r["updated_at"]} for r in
                self.store.q("SELECT key, operator, updated_at FROM settings_overrides WHERE section = ?", (TONES_SECTION,))}
        events = self._heard()
        book = self.tone_book(ov)
        from sizeup.radio.tones import TonePlan
        plan = TonePlan(book.codes())
        has_clip = getattr(self.cad, "has_clip", None)
        return {"entities": book.table(events, meta),
                "unnamed": tonelist.unnamed(events, plan, has_clip if callable(has_clip) else None),
                **self.tone_choices(book), "locked": sorted(self._locked()), "report": book.report(), "max": MAX_NAMED_TONES}

    def _list_row(self, key: str):
        return next((c for c in self.base_tones if tonelist.list_key(c) == key), None)

    def _tone_commit(self, changes: list, op: str, cur: dict) -> dict | None:
        changes = [c for c in changes if (c[2] is REMOVE and c[1] in cur) or (c[2] is not REMOVE and cur.get(c[1]) != c[2])]
        if not changes:
            return None
        return self._commit(changes, op, [("radios", f"tones/{k}", cur.get(k), None if v is REMOVE else v)
                                          for _s, k, v in changes])

    def _pair_value(self, book: tonelist.Book, key: str, cur: dict, places, service: str, role: str | None = None,
                    confirmed: bool | None = None, page: str = "") -> tuple[str, object]:
        """The override for a pair as it should be now (REMOVE when a list row is back to what the list says)."""
        p = book.pair(key)
        if p is None:
            raise CadError(f"no tone pair {key!r}")
        body = {"a_hz": p.a_hz, "b_hz": p.b_hz, "places": list(places), "service": service,
                "role": p.role if role is None else role}
        if p.origin != "named":
            body["list"] = p.list_name
        old = cur.get(key) or {}
        conf = old.get("confirmed") if confirmed is None else confirmed
        if conf is not None:
            body["confirmed"] = conf
        if p.origin == "named" and (page or old.get("page")):
            body["page"] = page or old["page"]
        k, val = self._clean_tone(body)
        orig = p.parsed
        if p.origin != "named" and orig is not None and self._as_listed(key, val, orig):
            return k, REMOVE
        return k, val

    def _as_listed(self, key: str, val: dict, orig) -> bool:
        """A list row's edit that says what the list says (no edit left)."""
        return (tonelist.ekey(val["places"], val["service"]) == tonelist.ekey(orig.places, orig.service)
                and val.get("role", "") == orig.role
                and val.get("confirmed", self._list_confirmed(key)) == self._list_confirmed(key))

    def _list_confirmed(self, key: str) -> bool:
        row = self._list_row(key)
        return row is not None and not row.name.endswith(" (est.)")

    def _entity_value(self, book: tonelist.Book, ekey: str, cur: dict, **change) -> tuple[str, object]:
        """The entity's override with these fields changed (REMOVE when nothing is left to keep)."""
        e = book.entities.get(ekey)
        old = dict(cur.get(tonelist.ENTITY + ekey) or {})
        if e is None and not old:
            raise CadError(f"no entity {ekey!r}")
        body = {k: v for k, v in old.items() if k not in ("places", "service")}
        body.update({"places": list(e.places if e else old["places"]), "service": e.service if e else old["service"]})
        for k, v in change.items():
            if v is None:
                body.pop(k, None)
            else:
                body[k] = v
        if e is not None and e.ours_locked:
            body.pop("ours", None)                # PCFD's / Rye Brook's: ours whatever is set
        k, val = self._clean_tone(body)
        return k, (REMOVE if set(val) <= {"places", "service"} else val)

    def _tones_op(self, action: str, body: dict, op: str) -> dict:
        """The tone book (#115, #122). set {a_hz, b_hz, places, service, role?, confirmed?, ours?, page?, list?}: a pair
        named or edited (page: the page it was heard on, read again; ours: its entity is ours) | move {key, places,
        service}: a pair to another entity (or a new one: split) | entity {key, ours?, notes?, aliases?} | merge {key,
        into: {places, service}}: an entity's pairs under another | delete {key}: forget a pair named here, take a list
        row out | reset {key}: a list row as the list has it, an entity as read | reread {page}: that page's recording
        through the tone decoder again (App.reread_tones; changes no setting)."""
        cur = self.tone_overrides()
        book = self.tone_book(cur)
        page, key = "", ""
        if action in ("delete", "reset"):
            key = _text(body.get("key"), "key", 200)
            row = self._list_row(key) if tonelist.is_list_key(key) else None
            if action == "reset" or row is None:
                if key not in cur:
                    raise CadError(f"no {'edit to' if action == 'reset' else 'named tones'} {key!r}")
                out = self._tone_commit([(TONES_SECTION, key, REMOVE)], op, cur)
            else:
                k, val = self._clean_tone({"list": key.split("@", 1)[1], "a_hz": row.a_hz, "b_hz": row.b_hz, "deleted": True})
                out = self._tone_commit([(TONES_SECTION, key, val)], op, cur)
        elif action == "set":
            key, val = self._clean_tone(body)
            if key not in cur and len(cur) >= MAX_NAMED_TONES:
                raise CadError(f"at most {MAX_NAMED_TONES} edited or named tone pairs")
            changes = []
            if "places" in val:
                ours = val.pop("ours", None)
                if "page" not in val and cur.get(key, {}).get("page") and not val.get("list"):
                    val["page"] = cur[key]["page"]        # an edit keeps the page it was named from
                row = self._list_row(key) if val.get("list") else None
                if row is not None and self._as_listed(key, val, tonelist.parse_name(val["list"])):
                    val = REMOVE                          # back to what the list says: no edit left
                changes.append((TONES_SECTION, key, val))
                ek = book.resolve(tonelist.ekey(body_places(body), body["service"]))
                if ours and ek not in book.locked_keys and not (book.entities.get(ek) and book.entities[ek].ours):
                    if ek in book.entities or tonelist.ENTITY + ek in cur:
                        changes.append((TONES_SECTION, *self._entity_value(book, ek, cur, ours=True)))
                    else:
                        changes.append((TONES_SECTION, *self._clean_tone({"places": body_places(body),
                                                                          "service": body["service"], "ours": True})))
            else:                                         # #84 / #115: by name, as before
                if val.get("ours") is False and val["name"] in self._locked():
                    val["ours"] = True                    # PCFD's / Rye Brook's name: ours whatever the box said
                if "page" not in val and cur.get(key, {}).get("page") and not val.get("list"):
                    val["page"] = cur[key]["page"]
                changes.append((TONES_SECTION, key, val))
            out = self._tone_commit(changes, op, cur)
            page = body.get("page") or "" if out else ""
        elif action == "move":
            key = _text(body.get("key"), "key", 200)
            places = body_places(body)
            k, val = self._pair_value(book, key, cur, places, tonelist._service(body.get("service"), "service"))
            out = self._tone_commit([(TONES_SECTION, k, val)], op, cur)
        elif action == "entity":
            key = _text(body.get("key"), "key", 200)
            change = {k: body[k] for k in ("ours", "notes", "aliases") if k in body}
            if change.get("notes") == "":
                change["notes"] = None
            if change.get("aliases") == []:
                change["aliases"] = None
            k, val = self._entity_value(book, key, cur, **change)
            out = self._tone_commit([(TONES_SECTION, k, val)], op, cur)
        elif action == "merge":
            key = _text(body.get("key"), "key", 200)
            into = body.get("into") if isinstance(body.get("into"), dict) else {}
            target = book.resolve(tonelist.ekey(body_places(into), tonelist._service(into.get("service"), "into.service")))
            if target == book.resolve(key):
                raise CadError("merge: into another entity")
            k, val = self._entity_value(book, key, cur, merged_into={"places": body_places(into), "service": into["service"]})
            out = self._tone_commit([(TONES_SECTION, k, val)], op, cur)
        elif action == "reread":
            pid = _text(body.get("page"), "page", 80)
            ctx = self.cad.radio_ctx
            if ctx is None or not hasattr(ctx, "reread_tones"):
                raise CadError("no radios here to read the page's tones again")
            try:
                return {"ok": True, **ctx.reread_tones(pid)}
            except ValueError as e:
                raise CadError(str(e)) from None
        else:
            raise CadError("action: set, move, entity, merge, delete, reset or reread")
        if out is None:
            return {"ok": True, "key": key}
        ctx = self.cad.radio_ctx
        if ctx is not None:
            ctx.tones_changed(page)
        return {**out, "key": key}

    def replace_tones(self, tones: dict, operator: str) -> bool:
        """The whole named-pairs map as the hub sent it (the station applying the CAD of record's names).
        True when anything changed. A bad entry: nothing changes (ValueError)."""
        want = {}
        for k, v in (tones or {}).items():
            try:
                key, val = self._clean_tone(v, f"tones.{k}")
            except CadError as e:
                raise ValueError(str(e)) from None
            want[key] = val
        cur = self.tone_overrides()
        changes = [(TONES_SECTION, k, REMOVE) for k in cur if k not in want]
        changes += [(TONES_SECTION, k, v) for k, v in want.items() if cur.get(k) != v]
        if not changes:
            return False
        self._commit(changes, operator, [("radios", f"tones/{k}", cur.get(k), None if v is REMOVE else v)
                                         for _s, k, v in changes])
        return True

    def stt_values(self) -> dict:
        return dict(self.overrides().get("stt") or {})

    def _vocab_list(self, v) -> list[str]:
        if not isinstance(v, list) or len(v) > MAX_VOCAB:
            raise CadError(f"vocabulary: a list of at most {MAX_VOCAB} phrases")
        out = []
        for i, x in enumerate(v):
            x = _text(x, f"vocabulary[{i}]", MAX_PHRASE_CHARS, required=False)
            x = " ".join(x.split())
            if x and x.lower() not in {y.lower() for y in out}:
                out.append(x)
        return out

    @staticmethod
    def _limit(v) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 10000:
            raise CadError("aws_monthly_limit_usd: 0 to 10000 (dollars)")
        return round(float(v), 2)

    def _stt_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: vocabulary | aws_monthly_limit_usd, value} | reset {key} | push (the vocabulary to AWS)."""
        ctx = self.cad.radio_ctx
        cur = self.stt_values()
        if action == "push":
            if ctx is None:
                raise CadError("no speech-to-text here")
            phrases = self.vocabulary()
            st = ctx.vocabulary_push(phrases)
            with self.store.tx() as db:
                self._audit(db, self.store.clock(), op, "radios", "vocabulary/push", None,
                            f"{len(phrases)} phrases: {st.get('state', '?')}")
            return {"ok": st.get("state") != "error", "vocabulary": st, **({"error": st["reason"]}
                                                                           if st.get("state") == "error" else {})}
        key = _text(body.get("key"), "key", 40)
        if key not in ("vocabulary", "aws_monthly_limit_usd"):
            raise CadError("key: vocabulary or aws_monthly_limit_usd")
        if action == "set":
            if key == "vocabulary":
                v = self._vocab_list(body.get("value"))
                dflt = ctx.vocabulary_suggestions() if ctx is not None else []
                new = REMOVE if v == dflt else v
                shown = (f"{len(cur[key])} phrases" if key in cur else "default"), f"{len(v)} phrases"
            else:
                v = self._limit(body.get("value"))
                dflt = ctx.aws_limit_default() if ctx is not None else 25.0
                new = REMOVE if v == dflt else v
                shown = cur.get(key, dflt), v
            out = self._commit([("stt", key, new)], op, [("radios", f"stt/{key}", *shown)])
        elif action == "reset":
            out = self._commit([("stt", key, REMOVE)], op, [("radios", f"stt/{key}", "edited", "default")])
        else:
            raise CadError("action: set, reset or push")
        if ctx is not None:
            ctx.channels_changed()
        return out

    def vocabulary(self) -> list[str]:
        v = self.stt_values().get("vocabulary")
        if isinstance(v, list):
            return [str(x) for x in v]
        ctx = self.cad.radio_ctx
        return list(ctx.vocabulary_suggestions()) if ctx is not None else []

    def aws_limit(self) -> float:
        v = self.stt_values().get("aws_monthly_limit_usd")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        ctx = self.cad.radio_ctx
        return ctx.aws_limit_default() if ctx is not None else 25.0

    def stt_page(self) -> dict:
        """GET /api/cad/stt: the vocabulary (as edited, as AWS gets it, its state there), the limit, AWS's use."""
        from sizeup.transcribe import build_vocabulary
        ctx = self.cad.radio_ctx
        cur = self.stt_values()
        phrases = self.vocabulary()
        built = build_vocabulary(phrases)
        return {"vocabulary": {"phrases": phrases, "edited": "vocabulary" in cur,
                               "suggestions": ctx.vocabulary_suggestions() if ctx is not None else [],
                               "aws": built["phrases"], "skipped": built["skipped"],
                               "sig": channels_mod.phrases_sig(built["phrases"]),
                               "state": ctx.vocabulary_state() if ctx is not None else {"state": "none"}},
                "limit": {"value": self.aws_limit(), "default": ctx.aws_limit_default() if ctx is not None else 25.0,
                          "edited": "aws_monthly_limit_usd" in cur},
                "aws": ctx.aws_status() if ctx is not None else {"enabled": False}}

    # -- the camera wall's area (#125): the margin around our fire districts, cameras always / never on it ---------
    # Kept as overrides (section "camera_wall": margin_m -> metres, include / exclude -> [camera ids]) so they are
    # audited, exported and imported with the rest; sizeup/roads.py Roads.area reads them on every request.

    def wall_values(self) -> dict:
        return dict(self.overrides().get(WALL_SECTION) or {})

    @staticmethod
    def _wall_margin(v) -> int:
        lo, hi, _ = WALL_MARGIN
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise CadError(f"margin_m: {lo} to {hi} (metres)")
        return int(round(v))

    @staticmethod
    def _wall_ids(v, what: str) -> list[str]:
        if not isinstance(v, list) or len(v) > MAX_WALL_IDS or not all(isinstance(x, str) and WALL_ID.fullmatch(x) for x in v):
            raise CadError(f"{what}: a list of camera ids (at most {MAX_WALL_IDS})")
        return sorted(set(v))

    def _wall_check(self, d: dict) -> None:
        if set(d) - set(WALL_KEYS):
            raise CadError(f"overrides.{WALL_SECTION}: {', '.join(WALL_KEYS)} only")
        if "margin_m" in d:
            self._wall_margin(d["margin_m"])
        for k in ("include", "exclude"):
            if k in d:
                self._wall_ids(d[k], f"overrides.{WALL_SECTION}.{k}")
        if set(d.get("include") or []) & set(d.get("exclude") or []):
            raise CadError(f"overrides.{WALL_SECTION}: a camera both included and excluded")

    def wall_page(self) -> dict:
        """Settings > System > Camera wall: the margin, every camera with where it falls (the app's Roads), the edits."""
        cur = self.wall_values()
        out = {"margin_m": cur.get("margin_m", WALL_MARGIN[2]), "default_margin_m": WALL_MARGIN[2], "max_margin_m": WALL_MARGIN[1],
               "include": list(cur.get("include") or []), "exclude": list(cur.get("exclude") or []), "edited": bool(cur),
               "cameras": None}
        fn = getattr(self.cad, "camera_area", None)
        if fn is not None:
            try:
                out |= {k: v for k, v in fn().items() if k in ("area", "cameras", "n", "streams")}
            except Exception as e:  # the cameras can't be listed now: the settings still can be edited
                out["error"] = f"{type(e).__name__}: {e}"
        return out

    def _wall_op(self, action: str, body: dict, op: str) -> dict:
        """set {key: margin_m, value} | camera {id, mode: auto | include | exclude} | reset (everything to the default)."""
        cur = self.wall_values()
        if action == "reset":
            if not cur:
                raise CadError("the camera wall is already the default")
            return self._commit([(WALL_SECTION, k, REMOVE) for k in cur], op, [("system", WALL_SECTION, cur, "default")])
        if action == "set":
            if body.get("key") != "margin_m":
                raise CadError("key: margin_m")
            v = self._wall_margin(body.get("value"))
            old = cur.get("margin_m", WALL_MARGIN[2])
            return self._commit([(WALL_SECTION, "margin_m", REMOVE if v == WALL_MARGIN[2] else v)], op,
                                [("system", f"{WALL_SECTION}/margin_m", old, v)])
        if action == "camera":
            cid = _text(body.get("id"), "id", 64)
            if not WALL_ID.fullmatch(cid):
                raise CadError("id: a camera id")
            mode = body.get("mode")
            if mode not in ("auto", "include", "exclude"):
                raise CadError("mode: auto, include or exclude")
            inc, exc = set(cur.get("include") or []), set(cur.get("exclude") or [])
            was = "include" if cid in inc else "exclude" if cid in exc else "auto"
            if was == mode:
                return {"ok": True}
            inc.discard(cid)
            exc.discard(cid)
            (inc if mode == "include" else exc if mode == "exclude" else set()).add(cid)
            if len(inc) > MAX_WALL_IDS or len(exc) > MAX_WALL_IDS:
                raise CadError(f"at most {MAX_WALL_IDS} cameras each way")
            return self._commit([(WALL_SECTION, "include", sorted(inc) or REMOVE), (WALL_SECTION, "exclude", sorted(exc) or REMOVE)], op,
                                [("system", f"{WALL_SECTION}/camera/{cid}", was, mode)])
        raise CadError("action: set, camera or reset")

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

    # last_seen / last_ip / last_ua (sizeup/cad/activity.py) are left out: where someone was is their
    # activity, an admin's to see (GET /api/cad/activity), not every reader of the Settings page's.
    OPERATOR_COLUMNS = "id, name, role, active, created_at, updated_at, email, callsign"

    def operators(self) -> list[dict]:
        rows = self.store.q(f"SELECT {self.OPERATOR_COLUMNS} FROM operators ORDER BY active DESC, name COLLATE NOCASE")
        for r in rows:
            r["active"] = bool(r["active"])
        return rows

    def operator_names(self) -> list[str]:
        return [r["name"] for r in self.store.q("SELECT name FROM operators WHERE active = 1 ORDER BY name COLLATE NOCASE")]

    # Signed in through Cloudflare Access (sizeup/cloud/access.py, 2026-10-07): the verified email is
    # who did it. What's recorded as the operator is "Name <email>" (just the email until someone
    # names it here), so the chronology reads "Corey" (cad.js shows the name, the email as its
    # tooltip) and the audit can't mix up two people with one name.

    @staticmethod
    def recorded(name: str, email: str) -> str:
        return email if not name or name.lower() == email else f"{name} <{email}>"

    def person(self, email: str) -> dict:
        """{email, name, callsign, operator} for a verified email. An email not on the list is added
        (named by its email, a dispatcher) so an admin can give it a name in Settings > Users."""
        email = email.strip().lower()
        if not EMAIL.fullmatch(email):
            raise CadError("not an email address")
        r = self.store.one("SELECT name, callsign FROM operators WHERE email = ?", (email,))
        if r is None:
            with self.store.tx() as db:
                row = db.execute("SELECT id, name, callsign, email FROM operators WHERE email = ? OR name = ?",
                                 (email, email)).fetchone()
                if row is None:
                    now = self.store.clock()
                    db.execute("INSERT INTO operators (name, role, active, created_at, updated_at, email) "
                               "VALUES (?, 'dispatcher', 1, ?, ?, ?)", (email, now, now, email))
                    self._audit(db, now, email, "users", email, None,
                                {"email": email, "seen": "signed in through Cloudflare Access (not named yet)"})
                    self.store.rev += 1
                elif not row["email"]:      # an operator named by the email itself, entered by hand
                    db.execute("UPDATE operators SET email = ? WHERE id = ?", (email, row["id"]))
            r = self.store.one("SELECT name, callsign FROM operators WHERE email = ?", (email,)) or {"name": email, "callsign": ""}
        return {"email": email, "name": r["name"], "callsign": r["callsign"], "operator": self.recorded(r["name"], email)}

    # Roles (user, 2026-10-08: "admins should be able to set users, dispatchers should not, but first
    # login should be able to set whatever i guess"): enforced only for a verified sign-in (the hub
    # with Cloudflare Access; sizeup/cad/api.py checks every POST). An admin changes anything; a
    # dispatcher runs calls but can't change Settings; a viewer looks. Deactivated: a viewer. Until
    # there is an active admin with a sign-in email, everyone signed in may do what an admin does, so
    # the first person can set things up (and nobody is locked out); after that the last such admin
    # can't be demoted, deactivated or imported away (LastAdmin).

    def admins(self) -> list[dict]:
        """The active admins with a sign-in email: the ones who count."""
        return [o for o in self.operators() if _is_admin(o)]

    def standing(self, email: str) -> dict:
        """person() plus {role, active, may, bootstrap, admins}: may = what they may do now (admin |
        dispatcher | viewer), bootstrap = no admin yet (anyone signed in may do anything)."""
        p = self.person(email)
        r = self.store.one("SELECT role, active FROM operators WHERE email = ?", (p["email"],)) or {"role": "dispatcher", "active": 1}
        admins = self.admins()
        active = bool(r["active"])
        may = "admin" if not admins else r["role"] if active and r["role"] in OPERATOR_ROLES else "viewer"
        return {**p, "role": r["role"], "active": active, "may": may, "bootstrap": not admins,
                "admins": [a["name"] for a in admins]}

    def whoami(self, email: str) -> dict:
        """GET /api/cad/whoami: the signed-in person ("access", roles enforced), or nobody known
        ("none": the Operator field is typed, as before, and roles are recorded, not enforced)."""
        if not email:
            return {"email": "", "name": "", "callsign": "", "source": "none", "role": "", "enforced": False}
        st = self.standing(email)
        return {k: st[k] for k in ("email", "name", "callsign", "role", "active", "may", "bootstrap", "admins")} | {
            "source": "access", "enforced": True}

    def _keep_an_admin(self, after: list[dict], why: str) -> None:
        """guard: the operators as they would be after a change still have an active admin with a
        sign-in email, if they have one now."""
        if self.admins() and not any(_is_admin(o) for o in after):
            raise LastAdmin(why)

    def _email(self, v, oid: int | None = None) -> str:
        v = _text(v, "email", 254, required=False).lower()
        if v and not EMAIL.fullmatch(v):
            raise CadError("email: the address they sign in with (name@example.org)")
        if v and self.store.one("SELECT 1 AS x FROM operators WHERE email = ? AND id != ?", (v, oid or -1)):
            raise CadError(f"{v} is already another operator's email")
        return v

    def _users_op(self, action: str, body: dict, op: str, guard: bool = False) -> dict:
        now = self.store.clock()

        def role_of(v, dflt):
            v = v or dflt
            if v not in OPERATOR_ROLES:
                raise CadError(f"role: one of {', '.join(OPERATOR_ROLES)}")
            return v
        if action == "add":
            name = _text(body.get("name"), "name", 60)
            role = role_of(body.get("role"), "dispatcher")
            email, callsign = self._email(body.get("email")), _text(body.get("callsign"), "callsign", 20, required=False)
            if self.store.one("SELECT 1 AS x FROM operators WHERE name = ?", (name,)):
                raise CadError(f"{name} is already an operator")
            with self.store.tx() as db:
                oid = db.execute("INSERT INTO operators (name, role, active, created_at, updated_at, email, callsign) "
                                 "VALUES (?, ?, 1, ?, ?, ?, ?)", (name, role, now, now, email, callsign)).lastrowid
                self._audit(db, now, op, "users", name, None, {"role": role, "active": True,
                                                                 **({"email": email} if email else {}),
                                                                 **({"callsign": callsign} if callsign else {})})
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
            if "email" in body:
                new["email"] = self._email(body.get("email"), oid)
            if "callsign" in body:
                new["callsign"] = _text(body.get("callsign"), "callsign", 20, required=False)
        elif action in ("deactivate", "activate"):
            new["active"] = action == "activate"
        else:
            raise CadError("action: add, edit, deactivate or activate")
        diff = {k: (cur[k], new[k]) for k in ("name", "role", "active", "email", "callsign") if cur[k] != new[k]}
        if not diff:
            return cur
        if guard and _is_admin(cur) and not _is_admin(new):
            self._keep_an_admin([new if o["id"] == oid else o for o in self.operators()],
                                f"{cur['name']} is the last admin: make someone else an admin first "
                                "(nobody could change Settings after that)")
        with self.store.tx() as db:
            db.execute("UPDATE operators SET name = ?, role = ?, active = ?, email = ?, callsign = ?, updated_at = ? WHERE id = ?",
                       (new["name"], new["role"], int(new["active"]), new["email"], new["callsign"], now, oid))
            self._audit(db, now, op, "users", cur["name"], {k: a for k, (a, _) in diff.items()}, {k: b for k, (_, b) in diff.items()})
        return self._operator(oid)

    def _operator(self, oid: int) -> dict:
        r = self.store.one(f"SELECT {self.OPERATOR_COLUMNS} FROM operators WHERE id = ?", (oid,))
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
                "run_cards": [{"type": c["type"], "spoken": c["spoken"], "is_default": c["is_default"], "slots": c["slots"],
                               "remarks": c["remarks"]} for c in runcards.cards(self.store)],
                "operators": [{"name": o["name"], "role": o["role"], "active": o["active"], "email": o["email"],
                               "callsign": o["callsign"]} for o in self.operators()]}

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
        if set(d) - {"tones", "channels", "voice", "speed", "weather_speak", *eta.KEYS}:
            raise CadError(f"overrides.dispatch: tones, channels, voice, speed, weather_speak and {', '.join(eta.KEYS)} only")
        if "weather_speak" in d and not isinstance(d["weather_speak"], bool):
            raise CadError("overrides.dispatch.weather_speak: true or false")
        for k, v in (ov.get(channels_mod.SECTION) or {}).items():
            try:
                channels_mod.clean(k, v)
            except ValueError as e:
                raise CadError(f"overrides.channels.{k}: {e}") from None
        for k, v in (ov.get(TONES_SECTION) or {}).items():
            key, val = self._clean_tone(v, f"overrides.tones.{k}")
            if key != k:
                raise CadError(f"overrides.tones.{k}: the key is the pair, {key}")
            ov[TONES_SECTION][k] = val
        self._wall_check(ov.get(WALL_SECTION) or {})
        st = ov.get("stt") or {}
        if set(st) - {"vocabulary", "aws_monthly_limit_usd"}:
            raise CadError("overrides.stt: vocabulary and aws_monthly_limit_usd only")
        if "vocabulary" in st:
            self._vocab_list(st["vocabulary"])
        if "aws_monthly_limit_usd" in st:
            self._limit(st["aws_monthly_limit_usd"])
        L = self.build(ov)
        for k in eta.KEYS:
            if k in d:
                try:
                    eta.check(k, d[k], L.free)
                except ValueError as e:
                    raise CadError(f"overrides.dispatch.{e}") from None
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
            fire_kinds = set(L.card_kinds)
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
                if sum(1 for sl in slots if isinstance(sl, dict) and runcards.ALL in sl) > 1:
                    raise CadError(f"run_cards[{i}]: one full department per card")
                for sl in slots:
                    if not isinstance(sl, dict):
                        raise CadError(f"run_cards[{i}]: a slot is an object")
                    if runcards.ALL in sl:
                        if sl[runcards.ALL] not in L.departments:
                            raise CadError(f"run_cards[{i}]: all: {' or '.join(L.departments)}")
                    elif sl.get("unit"):
                        if sl["unit"] not in known or L.response(sl["unit"]) == "agency":
                            raise CadError(f"run_cards[{i}]: {sl['unit']!r} is not a fire unit on the boards")
                    elif not (sl.get("kind") == lists_mod.ANY_KIND or set(str(sl.get("kind", "")).split("|")) <= fire_kinds) or not (
                            isinstance(sl.get("count", 1), int) and 1 <= sl.get("count", 1) <= runcards.MAX_COUNT):
                        raise CadError(f"run_cards[{i}]: kind {sl.get('kind')!r} x {sl.get('count')!r}: a fire apparatus kind, 1 to {runcards.MAX_COUNT}")
                c["type"] = k
        ops = data.get("operators")
        if ops is not None:
            if not isinstance(ops, list) or len(ops) > 500:
                raise CadError("operators: a list")
            names, emails = set(), set()
            for i, o in enumerate(ops):
                if not isinstance(o, dict):
                    raise CadError(f"operators[{i}]: an object")
                o["name"] = _text(o.get("name"), f"operators[{i}].name", 60)
                if o.get("role", "dispatcher") not in OPERATOR_ROLES or not isinstance(o.get("active", True), bool):
                    raise CadError(f"operators[{i}]: role one of {', '.join(OPERATOR_ROLES)}, active true/false")
                if o["name"].lower() in names:
                    raise CadError(f"operators: {o['name']} twice")
                names.add(o["name"].lower())
                o["email"] = _text(o.get("email"), f"operators[{i}].email", 254, required=False).lower()
                o["callsign"] = _text(o.get("callsign"), f"operators[{i}].callsign", 20, required=False)
                if o["email"] and not EMAIL.fullmatch(o["email"]):
                    raise CadError(f"operators[{i}].email: an email address")
                if o["email"] and o["email"] in emails:
                    raise CadError(f"operators: {o['email']} twice")
                emails.add(o["email"])
        return ov, cards, ops, L

    def _import_op(self, action: str, body: dict, op: str, guard: bool = False) -> dict:
        if action not in ("dry_run", "apply"):
            raise CadError("action: dry_run or apply")
        ov, cards, ops, L = self._check_import(body.get("settings"))
        if guard and ops is not None:         # the operators as apply (below) would leave them
            after = {o["name"]: dict(o) for o in self.operators()}
            for o in ops:
                for r in after.values():
                    if o["email"] and r["email"] == o["email"] and r["name"] != o["name"]:
                        r["email"] = ""
            for o in ops:
                after[o["name"]] = {"name": o["name"], "role": o.get("role", "dispatcher"), "active": o.get("active", True),
                                    "email": o["email"]}
            self._keep_an_admin(list(after.values()), "the file leaves no active admin with a sign-in email "
                                f"({', '.join(a['name'] for a in self.admins())} now): nobody could change Settings after it")
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
            have = {c["type"]: {"spoken": c["spoken"], "is_default": c["is_default"], "slots": c["slots"], "remarks": c["remarks"]}
                    for c in runcards.cards(self.store)}
            want = {c["type"]: {"spoken": str(c.get("spoken") or ""), "is_default": bool(c.get("is_default")),
                                "slots": [runcards.normal(s) for s in c["slots"]],
                                "remarks": " ".join(str(c.get("remarks") or "").split())[:runcards.MAX_REMARKS]} for c in cards}
            for k in sorted(set(have) | set(want)):
                if have.get(k) != want.get(k):
                    changes.append({"section": "run cards", "store": "run_cards", "key": k,
                                    "change": "added" if k not in have else "removed" if k not in want else "changed",
                                    "old": have.get(k), "new": want.get(k)})
        if ops is not None:
            have = {o["name"].lower(): o for o in self.operators()}
            for o in ops:
                h = have.get(o["name"].lower())
                new = {"role": o.get("role", "dispatcher"), "active": o.get("active", True), "email": o["email"],
                       "callsign": o["callsign"]}
                old = {k: h[k] for k in new} if h else None
                if old != new or h["name"] != o["name"]:
                    changes.append({"section": "users", "store": "operators", "key": o["name"],
                                    "change": "added" if h is None else "changed", "old": old, "new": new})
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
                    cid = db.execute("INSERT INTO run_cards (type, spoken, is_default, sort, remarks) VALUES (?, ?, ?, ?, ?)",
                                     (c["type"], str(c.get("spoken") or "")[:120], int(bool(c.get("is_default"))), i,
                                      " ".join(str(c.get("remarks") or "").split())[:runcards.MAX_REMARKS])).lastrowid
                    runcards._write_slots(db, cid, c["slots"])
            if ops is not None:
                for o in ops:      # an email the file gives someone else moves to them (the file wins)
                    if o["email"]:
                        db.execute("UPDATE operators SET email = '' WHERE email = ? AND name != ?", (o["email"], o["name"]))
                for o in ops:
                    row = db.execute("SELECT id FROM operators WHERE name = ?", (o["name"],)).fetchone()
                    if row:
                        db.execute("UPDATE operators SET name = ?, role = ?, active = ?, email = ?, callsign = ?, updated_at = ? "
                                   "WHERE id = ?", (o["name"], o.get("role", "dispatcher"), int(o.get("active", True)),
                                                    o["email"], o["callsign"], now, row[0]))
                    else:
                        db.execute("INSERT INTO operators (name, role, active, created_at, updated_at, email, callsign) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (o["name"], o.get("role", "dispatcher"),
                                                                    int(o.get("active", True)), now, now, o["email"], o["callsign"]))
            for c in changes:
                self._audit(db, now, op, c["section"], f"{c['store']}/{c['key']}" if c["store"] not in ("run_cards", "operators")
                            else c["key"], c["old"], c["new"])
            self._audit(db, now, op, "system", "import", None, f"{len(changes)} changes")
        self._use(L, ov)
        self.store.sync_units()
        self.store.rev += 1
        if self.cad.radio_ctx is not None and any(c["store"] in (channels_mod.SECTION, "stt") for c in changes):
            self.cad.radio_ctx.channels_changed()
        if self.cad.radio_ctx is not None and any(c["store"] == TONES_SECTION for c in changes):
            self.cad.radio_ctx.tones_changed()
        return {"ok": True, "dry_run": False, "changes": changes}
