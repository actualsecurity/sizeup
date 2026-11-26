"""Each person's own console choices, kept on the hub (#823): they follow the person to any browser or device.

    GET /api/me/prefs          {"signed_in": bool, "id": operator id | null, "prefs": {ns: value}}: the caller's own
    PUT /api/me/prefs/<ns>     the namespace's whole value (JSON): replaces it; {"ok": true, "prefs": {ns: value}};
                               X-Sizeup-Person: the id the page loaded for (another person signed in since: 409)

Keyed by the person's operator row (Settings > Users: the Cloudflare Access email's row, its id stable through a renamed
name or a changed email), never by anything the request says: one person can only read and write their own. A viewer
too (it is their own screen). Without a verified sign-in (all-in-one, local dev) there is nobody to keep them for: GET
says signed_in false and the page keeps them in the browser only; a PUT is refused.

Namespaces, each a known shape (anything else is refused, not trimmed):

    map_layers   {overview: {layer: bool}, incident: {layer: bool}, folds: {group: bool}, hideDown: bool}
                 (static/maplayers.js; a layer key "cameras", "gis:hydrants"; a fold is a Layers box group's heading)
    preplan      {layer: bool} (static/preplan.js: the Pre-plan layer on every map)
    reminders    {sound: bool, board_sound: bool} (static/reminders.js: a short chime when a reminder starts flashing,
                 on the console; board_sound: on /monitor, the wall board. Both off unless turned on)

An event's own layer set (incidents.map_layers, POST /api/cad/incident/<id>) is the event's, not here.

Stored in the CAD database (person_prefs: Litestream replicates it with everything else). A write is one transaction
under the store's lock without store.rev (as sizeup/cad/activity.py and sizeup/intel/events.py write): someone's
Layers box is no CAD change, so no console's state, no per-rev cache and no app read is rebuilt for it. A fenced hub
task (sizeup/fence.py, #501: not the leader) refuses the write with a 503 before trying it; the page keeps the
choice and sends it again. Never logged: the values are nobody's business but the person's.
"""

from __future__ import annotations

import json
import re

from sizeup import sourcekeys

SCHEMA = """
CREATE TABLE IF NOT EXISTS person_prefs (
  operator_id INTEGER NOT NULL REFERENCES operators(id) ON DELETE CASCADE,   -- whose: their Settings > Users row
  ns TEXT NOT NULL,                     -- map_layers | preplan (NAMESPACES)
  value TEXT NOT NULL,                  -- JSON, as validated
  updated_at REAL NOT NULL,
  PRIMARY KEY (operator_id, ns)
) WITHOUT ROWID
"""

PATH = "/api/me/prefs"
MAX_BODY = 16384               # a PUT's body
MAX_NS_BYTES = 16384           # one namespace's value, as stored
MAX_PERSON_BYTES = 32768       # all of a person's namespaces together
MAX_KEYS = 400                 # one set's entries (the county's GIS catalog is the biggest: under a hundred)
LAYER_KEY = re.compile(r"[a-z][a-z0-9_:-]{0,47}")       # maplayers.js KEY_RE
FOLD_MAX = 80                  # a Layers box group's heading (gislayers.js: the catalog's group names)
NS = re.compile(r"[a-z][a-z0-9_]{0,31}")
LISTEN_LEVELS = ("priority", "normal", "off")
LISTEN_MODES = ("interrupt", "duck", "queue")
LISTEN_AGE = (10, 600)         # max_age: a Normal transmission older than this (s) is dropped, never played late
LISTEN_TARGET = re.compile(r"ours|svc:[a-z]{2,12}|sys:[a-z][a-z0-9_-]{0,23}|mhz:\d{2,4}(?:\.\d{1,4})?")
LISTEN_SRC_MAX = 120           # a source key's length (radiofolders.KEY_MAX)
LISTEN_CHANNEL = re.compile(r"(?:fm|air):[^\x00-\x1f\x7f]{1,100}")      # an FM / airband channel (radiofolders.ITEM)


class PrefsError(ValueError):
    """A value refused (400) or too big (413): the reason, shown to nobody but the page's console."""

    def __init__(self, why: str, code: int = 400):
        super().__init__(why)
        self.code = code


def _bools(v, what: str, key_ok) -> dict:
    if not isinstance(v, dict):
        raise PrefsError(f"{what}: an object of true / false")
    if len(v) > MAX_KEYS:
        raise PrefsError(f"{what}: at most {MAX_KEYS} entries", 413)
    for k, b in v.items():
        if not key_ok(k):
            raise PrefsError(f"{what}: a bad key")
        if not isinstance(b, bool):
            raise PrefsError(f"{what}: true or false only")
    return dict(v)


def _fold_key(k: str) -> bool:
    """(maplayers.js FOLD_OK says the same, so a browser's old copy never makes the whole set refused)"""
    return 0 < len(k) <= FOLD_MAX and k.isprintable() and k == k.strip()


def _map_layers(v) -> dict:
    if not isinstance(v, dict):
        raise PrefsError("map_layers: an object")
    extra = set(v) - {"overview", "incident", "folds", "hideDown"}
    if extra:
        raise PrefsError("map_layers: overview, incident, folds and hideDown only")
    out: dict = {}
    for name in ("overview", "incident"):
        if name in v:
            out[name] = _bools(v[name], f"map_layers.{name}", lambda k: bool(LAYER_KEY.fullmatch(k)))
    if "folds" in v:
        out["folds"] = _bools(v["folds"], "map_layers.folds", _fold_key)
    if "hideDown" in v:
        if not isinstance(v["hideDown"], bool):
            raise PrefsError("map_layers.hideDown: true or false")
        out["hideDown"] = v["hideDown"]
    return out


def _preplan(v) -> dict:
    if not isinstance(v, dict) or set(v) - {"layer"}:
        raise PrefsError("preplan: {layer: true | false}")
    if "layer" in v and not isinstance(v["layer"], bool):
        raise PrefsError("preplan.layer: true or false")
    return dict(v)


def _reminders(v) -> dict:
    if not isinstance(v, dict) or set(v) - {"sound", "board_sound"}:
        raise PrefsError("reminders: {sound, board_sound: true | false}")
    if any(not isinstance(b, bool) for b in v.values()):
        raise PrefsError("reminders: true or false only")
    return dict(v)


def _listen_target(k) -> bool:
    if not isinstance(k, str):
        return False
    if k.startswith("src:"):
        key = k[4:]
        # (a source key's own shape, as sizeup/cad/radiofolders.py is_key: not imported, the store imports this)
        return 0 < len(key) <= LISTEN_SRC_MAX and key.isprintable() and key == key.strip() and (
            bool(LISTEN_CHANNEL.fullmatch(key)) or sourcekeys.parse(key) is not None)
    if k.startswith("svc:"):
        return k[4:] in sourcekeys.SERVICES
    return bool(LISTEN_TARGET.fullmatch(k))


def _listen(v) -> dict:
    if not isinstance(v, dict):
        raise PrefsError("listen: an object")
    extra = set(v) - {"rules", "mode", "max_age", "ours_only", "mute_police"}
    if extra:
        raise PrefsError("listen: rules, mode, max_age, ours_only and mute_police only")
    out: dict = {}
    if "rules" in v:
        r = v["rules"]
        if not isinstance(r, dict):
            raise PrefsError("listen.rules: {target: priority | normal | off}")
        if len(r) > MAX_KEYS:
            raise PrefsError(f"listen.rules: at most {MAX_KEYS} entries", 413)
        for k, lvl in r.items():
            if not _listen_target(k):
                raise PrefsError("listen.rules: a bad target")
            if lvl not in LISTEN_LEVELS:
                raise PrefsError(f"listen.rules: {', '.join(LISTEN_LEVELS)}")
        out["rules"] = dict(r)
    if "mode" in v:
        if v["mode"] not in LISTEN_MODES:
            raise PrefsError(f"listen.mode: {', '.join(LISTEN_MODES)}")
        out["mode"] = v["mode"]
    if "max_age" in v:
        a = v["max_age"]
        if isinstance(a, bool) or not isinstance(a, int) or not LISTEN_AGE[0] <= a <= LISTEN_AGE[1]:
            raise PrefsError(f"listen.max_age: whole seconds, {LISTEN_AGE[0]} to {LISTEN_AGE[1]}")
        out["max_age"] = a
    for b in ("ours_only", "mute_police"):
        if b in v:
            if not isinstance(v[b], bool):
                raise PrefsError(f"listen.{b}: true or false")
            out[b] = v[b]
    return out


def _radio_views(v) -> dict:
    from sizeup.cad import radioviews          # (here: store.py imports this module, radioviews store's CadError)
    from sizeup.cad.store import CadError
    try:
        return radioviews.clean_prefs(v)
    except CadError as e:
        raise PrefsError(str(e)) from None


NAMESPACES = {"map_layers": _map_layers, "preplan": _preplan, "reminders": _reminders, "listen": _listen,
              "radio_views": _radio_views}


def validate(ns: str, value) -> str:
    """The value as stored (compact JSON), or PrefsError."""
    check = NAMESPACES.get(ns)
    if check is None:
        raise PrefsError(f"namespace: one of {', '.join(NAMESPACES)}", 404)
    raw = json.dumps(check(value), separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    if len(raw.encode()) > MAX_NS_BYTES:
        raise PrefsError(f"{ns}: at most {MAX_NS_BYTES // 1024} KB", 413)
    return raw


class Prefs:
    """One per Cad (sizeup/cad/service.py)."""

    def __init__(self, store, settings=None):
        self.store, self.settings = store, settings

    def operator_id(self, email: str, add: bool = False) -> int | None:
        """The signed-in person's operator row; add: put them on the list first, as whoami does (the leader only)."""
        email = (email or "").strip().lower()
        if not email:
            return None
        r = self.store.one("SELECT id FROM operators WHERE email = ?", (email,))
        if r is None and add and self.settings is not None and self.store.may_write():
            self.settings.person(email)
            r = self.store.one("SELECT id FROM operators WHERE email = ?", (email,))
        return int(r["id"]) if r else None

    def all(self, oid: int) -> dict:
        out = {}
        for r in self.store.q("SELECT ns, value FROM person_prefs WHERE operator_id = ?", (oid,)):
            if r["ns"] in NAMESPACES:           # (a namespace a later build dropped: not handed out)
                try:
                    out[r["ns"]] = json.loads(r["value"])
                except ValueError:
                    pass
        return out

    def put(self, oid: int, ns: str, value) -> dict:
        """Replace one namespace; the value as stored. PrefsError: refused; nothing written."""
        raw = validate(ns, value)
        now = self.store.clock()
        db = self.store.db
        with self.store.lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                others = db.execute("SELECT COALESCE(SUM(LENGTH(CAST(value AS BLOB))), 0) FROM person_prefs "
                                    "WHERE operator_id = ? AND ns != ?", (oid, ns)).fetchone()[0]
                if others + len(raw.encode()) > MAX_PERSON_BYTES:
                    raise PrefsError(f"at most {MAX_PERSON_BYTES // 1024} KB of choices per person", 413)
                db.execute("INSERT INTO person_prefs (operator_id, ns, value, updated_at) VALUES (?, ?, ?, ?) "
                           "ON CONFLICT(operator_id, ns) DO UPDATE SET value = excluded.value, "
                           "updated_at = excluded.updated_at WHERE person_prefs.value != excluded.value",
                           (oid, ns, raw, now))
            except BaseException:
                if db.in_transaction:
                    try:
                        db.execute("ROLLBACK")
                    except Exception:  # noqa: BLE001 - the first error is the one raised
                        pass
                raise
            try:
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    try:
                        db.execute("ROLLBACK")
                    except Exception:  # noqa: BLE001
                        pass
                raise
        return json.loads(raw)


def _json(code: int, obj, headers: dict | None = None) -> tuple:
    return code, json.dumps(obj, separators=(",", ":")).encode(), "application/json", {
        "Cache-Control": "no-store", **(headers or {})}


def http_get(prefs: Prefs, who) -> tuple:
    """GET /api/me/prefs for who (the request's verified identity, or None). Read only: nothing is written."""
    email = getattr(who, "email", "") if who is not None else ""
    if not email:
        return _json(200, {"signed_in": False, "id": None, "prefs": {}})
    oid = prefs.operator_id(email)
    # not on the list yet (their first request: whoami adds them): nothing kept yet, and no id to keep it under
    return _json(200, {"signed_in": True, "id": oid, "prefs": prefs.all(oid) if oid is not None else {}})


def http_put(prefs: Prefs, who, path: str, raw: bytes | None, expect: str | None = None) -> tuple:
    """PUT /api/me/prefs/<ns> for who: their own namespace, replaced. expect (X-Sizeup-Person): the id the page loaded
    its choices for; someone else signed in on that browser since: 409, nothing written (not theirs to keep)."""
    email = getattr(who, "email", "") if who is not None else ""
    if not email:
        return _json(403, {"error": "nobody signed in: kept in this browser only"})
    ns = path[len(PATH) + 1:]
    if not NS.fullmatch(ns) or ns not in NAMESPACES:
        return _json(404, {"error": f"namespace: one of {', '.join(NAMESPACES)}"})
    if raw is None:
        return _json(413, {"error": f"at most {MAX_BODY // 1024} KB"})
    try:
        value = json.loads(raw)
    except ValueError:
        return _json(400, {"error": "a JSON body"})
    try:
        validate(ns, value)                    # refused before anything else (a fenced task says the same)
    except PrefsError as e:
        return _json(e.code, {"error": str(e)})
    if not prefs.store.may_write():            # not the leader (#501): the page sends it again
        return _json(503, {"error": "the hub is handing over: try again"}, {"Retry-After": "2"})
    oid = prefs.operator_id(email)
    if expect is not None and expect.strip() != str(oid):      # (checked before anyone is added to the list)
        return _json(409, {"error": "signed in as someone else: load again"})
    if oid is None:
        oid = prefs.operator_id(email, add=True)
    if oid is None:
        return _json(503, {"error": "try again"}, {"Retry-After": "2"})
    try:
        return _json(200, {"ok": True, "prefs": {ns: prefs.put(oid, ns, value)}})
    except PrefsError as e:
        return _json(e.code, {"error": str(e)})
