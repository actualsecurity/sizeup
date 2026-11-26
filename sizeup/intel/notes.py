"""Notes and tags on radio intel events (the admin, 2026-11-25: "a way to add notes, so we can take those notes per
incident and modify in the future how we look at or pull in that info and respond to it").

Many notes per event (intel_notes, sizeup/intel/events.py): free text, its author and time, structured tags (TAGS) and
optional corrections (category, place, level). Anyone signed in reads and adds them (as the Intel page and its votes);
the author edits and deletes their own, an admin any; only an admin marks one acted on ("done": "PR #123", "rule
added"). Every add, edit, delete and done is in intel_note_audit with the note before and after.

What reads them later:

    the Intel page        a count chip on the row; filters by note text (?q=), a tag (?tag=) and the review queue
                          (?review=1: a note tagged for a rule or model change, REVIEW_TAGS, not yet done), eventlog.py
    More info             every note in full, with the forms (static/intel-notes.js)
    the incident          the notes of the events linked to it (near_incident, incident_id): GET
                          /api/cad/incident/<id>/intel, the incident view's Intel notes
    tuning                each tag is also a verdict in intel_feedback, once per note (TAG_VERDICT; note_id its note,
                          traffic_id NULL; the rules list and the rule set's hash as the event had them), the store the
                          rules' and the model's tuning read; a note with corrections and no tag: one "corrected" row.
                          Rewritten on every edit, removed with the note; never counted as a person's vote (the Intel
                          page's and More info's votes are the rows with no note_id). false_garbled_audio also writes a
                          "garbled_tx" row per transmission (traffic_id set): the speech-to-text side's pointer to the
                          clips, labelled per transmission so a count of "garbled" stays one per note. There is no
                          speech-to-text feedback or label table to write to (stt_corrections are admins' corrected
                          transcripts, stt_runs.label_flag the time check's; the experiment log, #876, isn't built):
                          intel_feedback is where it is kept
    the clips             a noted event's transmissions are keepers while the note exists (sizeup/cad/comms.py: never
                          pruned, their audio a record, as a hand-corrected transmission's), so its pointers never go
                          stale
    the export            admins: GET /api/cad/intel/export.csv | .jsonl?from=&to= (and the Intel page's filters): each
                          event with its notes, tags, scores, rule hits, the model's record, verdicts and transcripts.
                          It holds names and what was said: it stays on the hub until an admin downloads it (in their
                          activity, kind "search")

The corrected place is picked as the incident editor's location is (static/cad.js addressBox: an address, an
intersection, "@" one of the department's common places; the qualifier in front, "IFO", from Settings > Lists >
Location qualifiers) and kept with the note (corrected_lat / _lon / _precision / _label / _qualifier / _place_key, and
heard: the words heard for it, "Sampel Diner"). corrected_place stays the place as written ("IFO 12 Sample Rd @ Sample
Diner"): an older note's free text alone, read as before. A picked place is:

    on the event          More info's Place card (context_bundle.py: place.corrected) and the Intel page's map
                          (eventlog.py map: corrected, drawn apart from the engine's pin, static/intel-map.js)
    a labelled example    one PLACE_EXAMPLE row in intel_feedback per note (the heard words -> the place picked; traffic_id
                          the transmission they were heard in), what tuning and replays read
    an alias              an admin's "add as alias" (POST /api/cad/intel/notes/<nid>/alias): the heard words become
                          another name of the common place picked (Settings: add_place_alias; the place search, the
                          radio's page locator and the county feed read them), so the next transmission saying them
                          resolves. Never another place's name or alias (409), a street's name, a number or a word
                          too common (alias_ok / alias_why say so first); {remove: true} takes it off again (as
                          Settings' Common places "−"). Audited in intel_note_audit (action "alias" / "unalias", the
                          same entry before and after) and in the settings audit
    a correction candidate  the misheard -> real pair (heard, the common place's name or the address picked) in
                          intel_correction_candidates, for the speech-to-text side (#956):

        correction_candidates(since=None, unimported_only=True) -> [{id, note_id, clip_id, heard_phrase, real_phrase,
                              kind, source, who, when, imported_at}], by when then id: the pairs written or changed
                              AFTER since (epoch seconds, exclusive: pass the last "when" read; None: all), only those
                              not taken yet unless unimported_only=False
        mark_imported(pairs) -> n  taken by the speech-to-text side (imported_at: now): each pair as read (a row
                              above, or (id, heard_phrase, real_phrase)); one changed since it was read is not marked
                              (it comes back with its new words and when). ids are never reused (AUTOINCREMENT)

    kind is place for a note's (place | unit | street | other); source "intel-note"; who the note's author. A note's edit
    changes its pair not yet taken in place (its words, clip and when), and only when the heard words, the real ones or
    the clip changed; a pair already taken is never rewritten nor written twice; its delete removes the one not taken.
    The hub's store by default (the IntelNotes the hub made, in the hub's process): in a process with none, a call
    without store= raises RuntimeError, so pass store= (a copy, tests) or call it in the hub. The phrases are never
    logged.

Notes are kept a year (events.py KEEP_NOTES_S), and an event with a note is kept with them; so are the pairs.
"""

from __future__ import annotations

import csv
import io
import json
import re
from contextlib import contextmanager

from sizeup.cad.store import CadError
from sizeup.intel.model import PRECISIONS

TAGS = ("should_have_pushed", "false_garbled_audio", "wrong_place", "wrong_category", "out_of_area", "late",
        "rule_to_add")
REVIEW_TAGS = ("rule_to_add", "wrong_category", "should_have_pushed", "false_garbled_audio")
TX_VERDICT = "garbled_tx"          # false_garbled_audio, per transmission (traffic_id): the speech-to-text side's
CORRECTED = "corrected"            # a note with corrections and no tag
PLACE_EXAMPLE = "place_example"    # a picked place: the heard words -> it (once per note; traffic_id where heard)
TAG_VERDICT = {"should_have_pushed": "should_push", "false_garbled_audio": "garbled", "wrong_place": "wrong_place",
               "wrong_category": "wrong_type", "out_of_area": "out_of_area", "late": "late", "rule_to_add": "rule_to_add"}
LEVELS = ("push", "map", "log", "none")
TEXT_MAX = 4000
PLACE_MAX = 200
DONE_MAX = 120
EXPORT_MAX = 5000
HEARD_MAX = 60
PICK = ("corrected_lat", "corrected_lon", "corrected_precision", "corrected_label", "corrected_place_key")
FIELDS = ("text", "tags", "corrected_category", "corrected_place", "corrected_level", *PICK, "corrected_qualifier",
          "heard")
# a location box suggestion's kind (static/cad.js addressBox) -> the engine's precision (model.PRECISIONS)
KIND_PRECISION = {"address": "address", "intersection": "intersection", "block": "block", "place": "landmark",
                  "landmark": "landmark", "mile marker": "landmark", "milemarker": "landmark", "exit": "landmark",
                  "street": "street", "town": "town"}
AREA = ((40.0, 42.0), (-75.0, -72.0))    # a point in the area (as Settings' place names)
CANDIDATE_KINDS = ("place", "unit", "street", "other")
CANDIDATE_SOURCE = "intel-note"
CANDIDATE_COLS = ("id", "note_id", "clip_id", "heard_phrase", "real_phrase", "kind", "source", "who", "when",
                  "imported_at")
_HUB: dict = {}                    # "store": the hub's CAD store (the IntelNotes it made), for the functions below


class Forbidden(CadError):
    """Not this person's to change (its author or an admin; done: admins): the API's 403, in their activity."""


def _who(operator: str) -> str:
    """"Name <email>" -> "Name" (the note's author as shown)."""
    o = operator or ""
    return o.split(" <", 1)[0].strip() if " <" in o else o


def _place_name(e: dict) -> str:
    """A place name's name as people write it: the county's label ("Sample Diner"), a hand-made one's name (its label
    is where it was picked)."""
    from sizeup.cad.commonplaces import is_imported
    return (e.get("label") if is_imported(e.get("key") or "", e) else "") or e.get("name") or ""


def _words(text: str) -> str:
    """How heard words and names are compared: lower case, letters and digits ("Sampel  Diner!" -> "sampel diner")."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


# -- the correction candidates (the speech-to-text side's, #956) ---------------------------------------------------------

def _store(store=None):
    s = store if store is not None else _HUB.get("store")
    if s is None:
        raise RuntimeError("no hub store: pass store=")
    return s


@contextmanager
def _tx(store):
    """One transaction under the store's lock, no CAD rev (an intel write, as SqlEvents._w)."""
    with store.lock:
        db = store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def correction_candidates(since: float | None = None, unimported_only: bool = True, *, store=None) -> list[dict]:
    """The misheard -> real pairs (the module doc), oldest first."""
    where, args = [], []
    if since is not None:
        where.append('"when" > ?')
        args.append(float(since))
    if unimported_only:
        where.append("imported_at IS NULL")
    cols = ", ".join(f'"{c}"' for c in CANDIDATE_COLS)
    sql = f"SELECT {cols} FROM intel_correction_candidates" + (f" WHERE {' AND '.join(where)}" if where else "")
    return [dict(r) for r in _store(store).q(sql + ' ORDER BY "when", id', args)]


def mark_imported(pairs, *, store=None, at: float | None = None) -> int:
    """The pairs taken by the speech-to-text side, each as it read it: a row of correction_candidates() or (id,
    heard_phrase, real_phrase). imported_at now (or at); a pair whose words changed since is left (not taken). -> how
    many were marked."""
    want = []
    for p in pairs or []:
        if isinstance(p, dict):
            want.append((int(p["id"]), str(p["heard_phrase"]), str(p["real_phrase"])))
        elif isinstance(p, (tuple, list)) and len(p) == 3:
            want.append((int(p[0]), str(p[1]), str(p[2])))
        else:
            raise TypeError("mark_imported: each pair as read: a row, or (id, heard_phrase, real_phrase)")
    if not want:
        return 0
    s = _store(store)
    when = float(at) if at is not None else float(s.clock())
    n = 0
    with _tx(s) as db:
        for i, heard, real in want:
            n += db.execute("UPDATE intel_correction_candidates SET imported_at = ? WHERE imported_at IS NULL AND id = ? "
                            "AND heard_phrase = ? AND real_phrase = ?", (when, i, heard, real)).rowcount
    return n


class IntelNotes:
    def __init__(self, cad, engine):
        self.cad, self.engine = cad, engine
        if getattr(cad, "store", None) is not None:
            _HUB["store"] = cad.store          # the functions above: the hub's (the last made: the live one)

    # -- reading ---------------------------------------------------------------------------------------------------

    @staticmethod
    def shown(r: dict, email: str = "", admin: bool = False) -> dict:
        try:
            tags = json.loads(r["tags"] or "[]")
        except ValueError:
            tags = []
        mine = bool(email) and (r["author_email"] or "").lower() == email.lower()
        return {"id": r["id"], "event_id": r["event_id"], "text": r["text"], "tags": tags,
                "corrected_category": r["corrected_category"], "corrected_place": r["corrected_place"],
                "corrected_level": r["corrected_level"], "corrected": IntelNotes.picked(r),
                "corrected_qualifier": r.get("corrected_qualifier") or "", "heard": r.get("heard") or "",
                "author": _who(r["author"]), "created_at": r["created_at"],
                "updated_at": r["updated_at"], "edited": r["updated_at"] > r["created_at"], "done": r["done"],
                "done_by": _who(r["done_by"]), "done_at": r["done_at"], "may_edit": mine or admin,
                "review": not r["done"] and any(t in REVIEW_TAGS for t in tags)}

    @staticmethod
    def picked(r: dict) -> dict | None:
        """The place picked ({lat, lon, precision, label, qualifier, place_key}); None: none (an older note's
        corrected_place is its free text alone)."""
        if r.get("corrected_lat") is None or r.get("corrected_lon") is None:
            return None
        return {"lat": r["corrected_lat"], "lon": r["corrected_lon"], "precision": r.get("corrected_precision") or "",
                "label": r.get("corrected_label") or "", "qualifier": r.get("corrected_qualifier") or "",
                "place_key": r.get("corrected_place_key") or ""}

    def _places(self) -> dict:
        """The department's place names by key (Settings; the county's common places and hand-made ones)."""
        try:
            return {r["key"]: r for r in self.cad.local_places() if r.get("key")}
        except Exception:  # noqa: BLE001 - no place names: none picked
            return {}

    @staticmethod
    def _names(e: dict) -> set:
        return {_words(x) for x in (e.get("name") or "", e.get("label") or "", *(e.get("aliases") or []))} - {""}

    def _with_place(self, n: dict, places: dict, admin: bool) -> dict:
        """A shown note with its common place's name (place_name) and, for an admin: whether its heard words may become
        an alias of it (alias_ok: picked, heard, not one of its names yet, and allowed: Settings alias_problem; else
        alias_why, the reason) and whether they are one now (alias_on: "− Alias")."""
        p = n.get("corrected")
        e = places.get(p["place_key"]) if p and p["place_key"] else None
        if e is not None:
            site = places.get(e.get("site") or "")
            n["place_name"] = _place_name(e)
            heard = _words(n["heard"])
            names = self._names(e) | (self._names(site) if site else set())
            aliases = {_words(a) for x in (e, site or {}) for a in (x.get("aliases") or [])}
            n["alias_on"] = bool(admin and heard and heard in aliases)
            why = None
            if admin and heard and heard not in names:
                try:
                    why = self.cad.settings.alias_problem(p["place_key"], n["heard"], places)
                except CadError as x:
                    why = ("bad", str(x))
            n["alias_ok"] = bool(admin and heard and heard not in names and not why)
            if why:
                n["alias_why"] = why[1]
        return n

    def of_event(self, eid: int, email: str = "", admin: bool = False) -> dict:
        """GET /api/cad/intel/<id>/notes: the event's notes, oldest first; the audit for an admin; the choices."""
        if self.cad.store.one("SELECT id FROM intel_events WHERE id = ?", (int(eid),)) is None:
            raise CadError(f"no pre-alert {eid}")
        rows = self.cad.store.q("SELECT * FROM intel_notes WHERE event_id = ? ORDER BY created_at, id", (int(eid),))
        places = self._places() if any(r.get("corrected_place_key") for r in rows) else {}
        out = {"notes": [self._with_place(self.shown(r, email, admin), places, admin) for r in rows], "tags": list(TAGS),
               "review_tags": list(REVIEW_TAGS), "levels": list(LEVELS), "categories": self.categories(), "admin": admin}
        if admin:
            out["audit"] = [{**a, "operator": _who(a["operator"])} for a in self.cad.store.q(
                "SELECT note_id, action, operator, at, before, after FROM intel_note_audit WHERE event_id = ? "
                "ORDER BY at, id", (int(eid),))]
        return out

    def categories(self) -> list[dict]:
        seen: dict = {}
        for p in getattr(self.engine, "packs", []) or []:
            for c, d in p.categories.items():
                seen.setdefault(c, d.get("label") or c)
        return [{"key": k, "label": v} for k, v in seen.items()]

    def counts(self, ids: list[int]) -> dict:
        """{event id: {n, tags, review}} for a page of events (the Intel page's chip)."""
        if not ids:
            return {}
        out: dict = {}
        for r in self.cad.store.q(f"SELECT event_id, tags, done FROM intel_notes WHERE event_id IN ({','.join('?' * len(ids))})",
                                  list(ids)):
            o = out.setdefault(r["event_id"], {"n": 0, "tags": [], "review": 0})
            o["n"] += 1
            try:
                tags = json.loads(r["tags"] or "[]")
            except ValueError:
                tags = []
            o["tags"] = sorted(set(o["tags"]) | set(tags))
            if not r["done"] and any(t in REVIEW_TAGS for t in tags):
                o["review"] += 1
        return out

    def of_incident(self, iid: int) -> dict:
        """GET /api/cad/incident/<id>/intel: the intel events linked to one of our incidents (near_incident: at its
        place; incident_id: accepted into it) and their notes."""
        evs = self.cad.store.q("SELECT id, pack, category, first_at, mode, pushed_at, suppressed, status, label_full "
                               "FROM intel_events WHERE near_incident = ? OR incident_id = ? ORDER BY first_at LIMIT 50",
                               (int(iid), int(iid)))
        ids = [e["id"] for e in evs]
        notes = self.cad.store.q(f"SELECT * FROM intel_notes WHERE event_id IN ({','.join('?' * len(ids))}) "
                                 "ORDER BY created_at, id", ids) if ids else []
        out = []
        for e in evs:
            p = self.engine.pack(e["pack"]) if hasattr(self.engine, "pack") else None
            label = (p.category(e["category"]).get("label") if p else "") or e["category"]
            out.append({"id": e["id"], "category": e["category"], "label": label, "at": e["first_at"],
                        "level": "push" if e["pushed_at"] is not None else "held" if e["mode"] == "push" else e["mode"],
                        "status": e["status"], "place": e["label_full"]})
        return {"events": out, "notes": [self.shown(n) for n in notes]}

    # -- writing -----------------------------------------------------------------------------------------------------

    def _clean(self, body: dict, cur: dict | None = None) -> dict:
        out = {k: cur.get(k) for k in FIELDS} if cur else {
            "text": "", "tags": "[]", "corrected_category": "", "corrected_place": "", "corrected_level": "",
            "corrected_lat": None, "corrected_lon": None, "corrected_precision": "", "corrected_label": "",
            "corrected_place_key": "", "corrected_qualifier": "", "heard": ""}
        for k in ("corrected_precision", "corrected_label", "corrected_place_key", "corrected_qualifier", "heard"):
            out[k] = out[k] or ""                         # (an older row read before its columns were filled)
        if "text" in body:
            t = str(body.get("text") or "").strip()
            if len(t) > TEXT_MAX:
                raise CadError(f"text: at most {TEXT_MAX} characters")
            out["text"] = t
        if "tags" in body:
            tags = body.get("tags") or []
            if not isinstance(tags, list) or any(t not in TAGS for t in tags):
                raise CadError(f"tags: any of {' | '.join(TAGS)}")
            out["tags"] = json.dumps([t for t in TAGS if t in tags])
        if "corrected_category" in body:
            c = str(body.get("corrected_category") or "")
            if c and c not in {x["key"] for x in self.categories()}:
                raise CadError("corrected_category: one of the packs' categories")
            out["corrected_category"] = c
        if "corrected_place" in body:
            out["corrected_place"] = str(body.get("corrected_place") or "").strip()[:PLACE_MAX]
        if "corrected_pick" in body:
            out.update(self._pick(body.get("corrected_pick")))
            if out["corrected_lat"] is not None and not out["corrected_place"]:
                out["corrected_place"] = out["corrected_label"][:PLACE_MAX]
        if "corrected_qualifier" in body:
            try:
                out["corrected_qualifier"] = self.cad.lists.qualifier_code(str(body.get("corrected_qualifier") or ""),
                                                                           (cur or {}).get("corrected_qualifier") or "")
            except ValueError as e:
                raise CadError(f"corrected_qualifier: {e}") from None
        if "heard" in body:
            out["heard"] = " ".join(str(body.get("heard") or "").split())[:HEARD_MAX]
        if "corrected_level" in body:
            lv = str(body.get("corrected_level") or "")
            if lv and lv not in LEVELS:
                raise CadError(f"corrected_level: {' | '.join(LEVELS)}")
            out["corrected_level"] = lv
        if not out["text"] and out["tags"] == "[]" and not (out["corrected_category"] or out["corrected_place"]
                                                             or out["corrected_level"] or out["corrected_lat"] is not None):
            raise CadError("a note needs text, a tag or a correction")
        return out

    def _pick(self, p) -> dict:
        """corrected_pick: a location box suggestion taken ({lat, lon, label, kind | precision, place_key?}) -> the
        note's columns; null / {}: none (the picked place cleared, corrected_place kept as written)."""
        none = {"corrected_lat": None, "corrected_lon": None, "corrected_precision": "", "corrected_label": "",
                "corrected_place_key": ""}
        if not p:
            return none
        if not isinstance(p, dict):
            raise CadError("corrected_pick: {lat, lon, label, kind, place_key?} or null")
        try:
            lat, lon = float(p.get("lat")), float(p.get("lon"))
        except (TypeError, ValueError):
            raise CadError("corrected_pick: lat and lon") from None
        if not (AREA[0][0] <= lat <= AREA[0][1] and AREA[1][0] <= lon <= AREA[1][1]):
            raise CadError("corrected_pick: lat and lon in the area")
        label = " ".join(str(p.get("label") or "").split())[:PLACE_MAX]
        if not label:
            raise CadError("corrected_pick: its label")
        key = str(p.get("place_key") or "")[:200]
        if key and key not in self._places():
            raise CadError("corrected_pick: no such common place")
        prec = str(p.get("precision") or "")
        if prec not in PRECISIONS or prec == "none":
            prec = KIND_PRECISION.get(str(p.get("kind") or ""), "landmark" if key else "address")
        return {"corrected_lat": round(lat, 6), "corrected_lon": round(lon, 6), "corrected_precision": prec,
                "corrected_label": label, "corrected_place_key": key}

    def _event(self, eid: int) -> dict:
        r = self.cad.store.one("SELECT id, rules, ruleset_hash, traffic_ids FROM intel_events WHERE id = ?", (int(eid),))
        if r is None:
            raise CadError(f"no pre-alert {eid}")
        return r

    def _note(self, nid: int) -> dict:
        r = self.cad.store.one("SELECT * FROM intel_notes WHERE id = ?", (int(nid),))
        if r is None:
            raise CadError(f"no note {nid}")
        return r

    @staticmethod
    def _may(r: dict, email: str, admin: bool) -> None:
        if not admin and not (email and (r["author_email"] or "").lower() == email.lower()):
            raise Forbidden("only its author or an admin may change a note")

    @staticmethod
    def _clip(db, note: dict, tids: list) -> str:
        """The transmission the heard words are in (its text or its corrected text, as words); the event's only one
        when none says them; else ''."""
        heard = _words(note.get("heard") or "")
        tids = [str(t) for t in tids or []]
        if heard and tids:
            cols = {r[1] for r in db.execute("PRAGMA table_info(radio_traffic)")}
            text = "text || ' ' || COALESCE(corr_text, '')" if "corr_text" in cols else "text"
            for k in range(0, len(tids), 500):
                part = tids[k:k + 500]
                got = {str(r[0]): r[1] for r in db.execute(f"SELECT id, {text} FROM radio_traffic WHERE id IN "
                                                           f"({','.join('?' * len(part))})", part)}
                for t in part:
                    if t in got and f" {heard} " in f" {_words(got[t])} ":
                        return t
        return tids[0] if len(tids) == 1 else ""

    def _feedback(self, db, note: dict, ev: dict, at: float) -> str:
        """The note's tags as intel_feedback verdicts (the tuning's store), replacing what it wrote before; a picked
        place: one PLACE_EXAMPLE row (the heard words -> the place, traffic_id where they were heard). -> that clip."""
        db.execute("DELETE FROM intel_feedback WHERE note_id = ?", (note["id"],))
        try:
            tags = json.loads(note["tags"] or "[]")
            tids = json.loads(ev["traffic_ids"] or "[]")
        except ValueError:
            tags, tids = [], []
        corrected = {k: note[f"corrected_{k}"] for k in ("place", "level") if note[f"corrected_{k}"]}
        point = self.picked(note)
        if point:
            corrected["point"] = point
        rows = [(TAG_VERDICT[t], None) for t in tags]                      # once per note
        if not tags and (corrected or note["corrected_category"]):         # (heard words alone: no correction)
            rows.append((CORRECTED, None))
        if note.get("heard") and corrected:
            corrected["heard"] = note["heard"]
        if "false_garbled_audio" in tags:
            rows += [(TX_VERDICT, tid) for tid in tids]                      # the clips, labelled per transmission
        clip = self._clip(db, note, tids) if point or note.get("heard") else ""
        if point:                                                          # the labelled example: heard -> picked
            rows.append((PLACE_EXAMPLE, clip or None))
        for v, tid in rows:
            db.execute("INSERT INTO intel_feedback (event_id, traffic_id, verdict, corrected, corrected_category, rules, "
                       "ruleset_hash, operator, at, note_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (note["event_id"], tid, v, json.dumps(corrected) if corrected else "",
                        note["corrected_category"][:40], ev["rules"] or "[]", ev["ruleset_hash"] or "",
                        note["author"][:120], at, note["id"]))
        return clip

    def _real(self, note: dict, places: dict) -> str:
        """What the heard words should have been: the common place's name, else the address picked (its first part)."""
        e = places.get(note.get("corrected_place_key") or "")
        if e is not None:
            return _place_name(e)
        if note.get("corrected_lat") is not None:
            return (note.get("corrected_label") or "").split(",")[0].strip()
        return ""

    def _candidates(self, db, note: dict, clip: str, at: float) -> None:
        """The note's misheard -> real pair (intel_correction_candidates): the one not taken yet changed in place, and
        only when its words or clip changed (else left as it is: its who and when too); none when the words are the
        same; a pair taken already is never rewritten nor written twice. who: the note's author."""
        heard = note.get("heard") or ""
        real = self._real(note, self._places() if note.get("corrected_place_key") else {}) if heard else ""
        real = real[:PLACE_MAX]
        rows = [dict(r) for r in db.execute("SELECT id, clip_id, heard_phrase, real_phrase, imported_at FROM "
                                            "intel_correction_candidates WHERE note_id = ? ORDER BY id", (note["id"],))]
        open_ = [r for r in rows if r["imported_at"] is None]
        want = bool(heard and real and _words(heard) != _words(real))
        if want and any(r["heard_phrase"] == heard and r["real_phrase"] == real for r in rows if r["imported_at"]):
            want = False                                                   # taken already: never twice
        if not want:
            for r in open_:
                db.execute("DELETE FROM intel_correction_candidates WHERE id = ?", (r["id"],))
            return
        for r in open_[1:]:                                                # (one per note)
            db.execute("DELETE FROM intel_correction_candidates WHERE id = ?", (r["id"],))
        if open_:
            r = open_[0]
            if (r["heard_phrase"], r["real_phrase"], r["clip_id"]) != (heard, real, clip or ""):
                db.execute('UPDATE intel_correction_candidates SET heard_phrase = ?, real_phrase = ?, clip_id = ?, '
                           '"when" = ? WHERE id = ?', (heard, real, clip or "", at, r["id"]))
            return
        db.execute('INSERT INTO intel_correction_candidates (note_id, clip_id, heard_phrase, real_phrase, kind, source, '
                   'who, "when") VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   (note["id"], clip or "", heard, real, "place", CANDIDATE_SOURCE, (note["author"] or "")[:160], at))

    @staticmethod
    def _audit(db, nid: int, eid: int, action: str, op: str, at: float, before, after) -> None:
        db.execute("INSERT INTO intel_note_audit (note_id, event_id, action, operator, at, before, after) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (nid, eid, action, op[:160], at,
                                                    json.dumps(before) if before else "", json.dumps(after) if after else ""))

    def add(self, eid: int, body: dict) -> dict:
        ev = self._event(eid)
        n = self._clean(body)
        op, email = str(body.get("operator") or "")[:160], str(body.get("_email") or "")[:160]
        now = self.cad.clock()
        admin = body.get("_may") == "admin"
        with self.engine.events._w(placed=n["corrected_lat"] is not None) as db:
            cols = ("event_id", *FIELDS, "author", "author_email", "created_at", "updated_at")
            cur = db.execute(f"INSERT INTO intel_notes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             (int(eid), *(n[k] for k in FIELDS), op, email, now, now))
            nid = int(cur.lastrowid)
            note = dict(db.execute("SELECT * FROM intel_notes WHERE id = ?", (nid,)).fetchone())
            clip = self._feedback(db, note, ev, now)
            self._candidates(db, note, clip, now)
            self._audit(db, nid, int(eid), "add", op, now, None, {k: note[k] for k in FIELDS})
        return {"ok": True, "note": self._with_place(self.shown(note, email, admin), self._places(), admin)}

    def edit(self, nid: int, body: dict) -> dict:
        op, email = str(body.get("operator") or "")[:160], str(body.get("_email") or "")
        admin = body.get("_may") == "admin"
        now = self.cad.clock()
        moved = False                                  # a picked place changed: the Intel page's map reads again
        with self.engine.events._w() as db:            # read and written in one transaction: never an older copy
            r = db.execute("SELECT * FROM intel_notes WHERE id = ?", (int(nid),)).fetchone()
            if r is None:
                raise CadError(f"no note {nid}")
            cur = dict(r)
            self._may(cur, email, admin)
            e = db.execute("SELECT id, rules, ruleset_hash, traffic_ids FROM intel_events WHERE id = ?",
                           (cur["event_id"],)).fetchone()
            ev = dict(e) if e else {"id": cur["event_id"], "rules": "[]", "ruleset_hash": "", "traffic_ids": "[]"}
            before = {k: cur.get(k) for k in FIELDS}
            if body.get("delete"):
                db.execute("DELETE FROM intel_notes WHERE id = ?", (int(nid),))
                db.execute("DELETE FROM intel_feedback WHERE note_id = ?", (int(nid),))
                db.execute("DELETE FROM intel_correction_candidates WHERE note_id = ? AND imported_at IS NULL", (int(nid),))
                self._audit(db, int(nid), cur["event_id"], "delete", op, now, {**before, "author": cur["author"]}, None)
                moved = before["corrected_lat"] is not None
                out = {"ok": True, "deleted": int(nid)}
            else:
                n = self._clean(body, cur)
                if n == before:                        # nothing changed: no write, no audit row
                    return {"ok": True, "note": self.shown(cur, email, admin), "unchanged": True}
                db.execute(f"UPDATE intel_notes SET {', '.join(f'{k} = ?' for k in FIELDS)}, updated_at = ? WHERE id = ?",
                           (*(n[k] for k in FIELDS), now, int(nid)))
                note = dict(db.execute("SELECT * FROM intel_notes WHERE id = ?", (int(nid),)).fetchone())
                clip = self._feedback(db, note, ev, now)
                self._candidates(db, note, clip, now)
                self._audit(db, int(nid), cur["event_id"], "edit", op, now, before, {k: note[k] for k in FIELDS})
                moved = any(before[k] != note[k] for k in PICK)
                out = None
        if moved:
            self.engine.events.map_rev += 1
        if out is not None:
            return out
        return {"ok": True, "note": self._with_place(self.shown(note, email, admin), self._places(), admin)}

    def alias(self, nid: int, body: dict) -> dict:
        """An admin's "add as alias of <common place>": the note's heard words become another name of the common place
        it picked (Settings add_place_alias), so the next transmission saying them resolves; {remove: true}: taken off
        again (remove_place_alias). Never another place's name or alias (settings.PlaceNameTaken: 409), nor a street,
        a number or a word too common (400). Audited here (action "alias" / "unalias": the entry it went on, before and
        after) and with the lists."""
        if body.get("_may") != "admin":
            raise Forbidden("changing a place's aliases is for admins")
        cur = self._note(nid)
        heard, key = cur.get("heard") or "", cur.get("corrected_place_key") or ""
        if not heard or not key:
            raise CadError("the note needs the words heard and a common place picked")
        if self._places().get(key) is None:
            raise CadError("its common place is gone")
        op = str(body.get("operator") or "")[:160]
        remove = bool(body.get("remove"))
        st = self.cad.settings
        out = (st.remove_place_alias if remove else st.add_place_alias)(key, heard, op)
        changed = out["removed"] if remove else out["added"]
        if changed:                                    # before and after: the same entry (a site's primary)
            now = self.cad.clock()
            with self.engine.events._w(listed=False) as db:
                self._audit(db, int(nid), cur["event_id"], "unalias" if remove else "alias", op, now,
                            {"place_key": out["key"], "aliases": out["had"]},
                            {"place_key": out["key"], "aliases": out["aliases"]})
        email = str(body.get("_email") or "")
        return {"ok": True, ("removed" if remove else "added"): changed, "place": out["name"],
                "note": self._with_place(self.shown(self._note(nid), email, True), self._places(), True)}

    def done(self, nid: int, body: dict) -> dict:
        """An admin's "done / PR #": the change it asked for is made ("" again: not yet)."""
        if body.get("_may") != "admin":
            raise Forbidden("marking a note done is for admins")
        cur = self._note(nid)
        d = str(body.get("done") or "").strip()[:DONE_MAX]
        op = str(body.get("operator") or "")[:160]
        now = self.cad.clock()
        with self.engine.events._w() as db:
            db.execute("UPDATE intel_notes SET done = ?, done_by = ?, done_at = ? WHERE id = ?",
                       (d, op if d else "", now if d else None, int(nid)))
            self._audit(db, int(nid), cur["event_id"], "done", op, now, {"done": cur["done"]}, {"done": d})
        return {"ok": True, "note": self.shown(self._note(nid), str(body.get("_email") or ""), True)}

    # -- the export (admins) ---------------------------------------------------------------------------------------

    def export(self, q: dict, fmt: str) -> tuple[bytes, int]:
        """Each event of the Intel page's filters (every page, at most EXPORT_MAX) with its notes, tags, scores, rule
        hits, the model's record, verdicts and transcripts: (the file, how many events)."""
        q = {k: v for k, v in q.items() if k not in ("page", "size")}
        il = self.cad.intel_admin.eventlog
        ids = il.ids(q, EXPORT_MAX)
        rows = self._export_rows(ids)
        from sizeup.cad.export import _cell
        if fmt == "jsonl":
            return "".join(json.dumps(r, separators=(",", ":"), default=str) + "\n" for r in rows).encode(), len(rows)
        out = io.StringIO()
        cols = ["id", "first_at", "last_at", "status", "pack", "category", "level", "would", "held", "score", "score_level",
                "where", "precision", "place", "town", "lat", "lon", "source", "source_key", "decided", "rules", "hits",
                "model", "pushed_at", "feedback", "tags", "notes", "transcripts"]
        w = csv.writer(out)
        w.writerow(cols)
        for r in rows:
            flat = dict(r)
            flat["rules"] = "; ".join(f"{x[0]} {x[2] if len(x) > 2 else ''}".strip() for x in r["rules"])
            flat["hits"] = "; ".join(f"{h['traffic_id']}:{h['rule_id']} {h['weight']}" for h in r["hits"])
            flat["model"] = json.dumps(r["model"]) if r["model"] else ""
            flat["feedback"] = "; ".join(f"{f['verdict']} ({f['operator']})" for f in r["feedback"])
            flat["tags"] = " ".join(sorted({t for n in r["notes"] for t in n["tags"]}))
            flat["notes"] = " || ".join(
                f"{n['author']}: {n['text']}" + (f" [{' '.join(n['tags'])}]" if n["tags"] else "")
                + "".join(f" ({k} -> {n['corrected_' + k]})" for k in ("category", "place", "level") if n["corrected_" + k])
                + (f" done: {n['done']}" if n["done"] else "") for n in r["notes"])
            flat["transcripts"] = " || ".join(f"{t['id']}: {t['text']}" for t in r["transcripts"])
            w.writerow([_cell(flat.get(c)) if flat.get(c) is not None else "" for c in cols])   # (never a formula)
        return out.getvalue().encode(), len(rows)

    def _export_rows(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        st = self.cad.store
        marks = ",".join("?" * len(ids))
        evs = {r["id"]: r for r in st.q(f"SELECT * FROM intel_events WHERE id IN ({marks})", ids)}
        notes: dict = {}
        for n in st.q(f"SELECT * FROM intel_notes WHERE event_id IN ({marks}) ORDER BY created_at, id", ids):
            notes.setdefault(n["event_id"], []).append(self.shown(n))
        fb: dict = {}
        for f in st.q(f"SELECT event_id, traffic_id, verdict, corrected, corrected_category, operator, at, note_id "
                      f"FROM intel_feedback WHERE event_id IN ({marks}) ORDER BY at, id", ids):
            fb.setdefault(f["event_id"], []).append({k: f[k] for k in f if k != "event_id"})
        tids: dict = {}
        for i, e in evs.items():
            try:
                tids[i] = json.loads(e["traffic_ids"] or "[]")
            except ValueError:
                tids[i] = []
        every = sorted({t for ts in tids.values() for t in ts})
        texts, hits = {}, {}
        for k in range(0, len(every), 500):
            part = every[k:k + 500]
            pm = ",".join("?" * len(part))
            for t in st.q(f"SELECT id, start, channel, text FROM radio_traffic WHERE id IN ({pm})", part):
                texts[t["id"]] = t
            for h in st.q(f"SELECT traffic_id, rule_id, weight, event_id FROM intel_hits WHERE traffic_id IN ({pm})", part):
                hits.setdefault(h["traffic_id"], []).append(h)
        b = getattr(self.engine, "backing", None)
        out = []
        from sizeup.intel.eventlog import source_name, source_names
        names = source_names(self.cad)
        listed = {r["id"]: r for r in self.cad.intel_admin.eventlog.rows_by_id(ids)}
        for i in ids:
            e = evs.get(i)
            if e is None:
                continue
            try:
                rules = json.loads(e["rules"] or "[]")
            except ValueError:
                rules = []
            model = None
            if b is not None:
                try:
                    model = b.shown(int(i))
                except Exception:  # noqa: BLE001 - the export never fails on the model's record
                    model = None
            lr = listed.get(i) or {}
            out.append({
                "id": i, "first_at": e["first_at"], "last_at": e["last_at"], "status": e["status"], "pack": e["pack"],
                "category": e["category"], "level": lr.get("level", e["mode"]), "would": lr.get("would", ""),
                "held": (lr.get("held") or {}).get("key", ""), "score": e["what_score"], "score_level": e["level"],
                "where": e["where_score"], "precision": e["precision"], "place": e["label_full"], "town": e["town"],
                "lat": e["lat"], "lon": e["lon"], "source": source_name(names, e["source_key"]),
                "source_key": e["source_key"], "decided": lr.get("decided", ""), "rules": rules,
                "hits": [{"traffic_id": h["traffic_id"], "rule_id": h["rule_id"], "weight": h["weight"]}
                         for t in tids[i] for h in hits.get(t, [])],
                "model": model, "pushed_at": e["pushed_at"], "suppressed": e["suppressed"],
                "feedback": fb.get(i, []), "notes": notes.get(i, []),
                "transcripts": [{"id": t, "at": texts[t]["start"], "channel": texts[t]["channel"], "text": texts[t]["text"]}
                                if t in texts else {"id": t, "at": None, "channel": "", "text": ""} for t in tids[i]]})
        return out
