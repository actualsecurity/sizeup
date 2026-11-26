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

Notes are kept a year (events.py KEEP_NOTES_S), and an event with a note is kept with them.
"""

from __future__ import annotations

import csv
import io
import json

from sizeup.cad.store import CadError

TAGS = ("should_have_pushed", "false_garbled_audio", "wrong_place", "wrong_category", "out_of_area", "late",
        "rule_to_add")
REVIEW_TAGS = ("rule_to_add", "wrong_category", "should_have_pushed", "false_garbled_audio")
TX_VERDICT = "garbled_tx"          # false_garbled_audio, per transmission (traffic_id): the speech-to-text side's
CORRECTED = "corrected"            # a note with corrections and no tag
TAG_VERDICT = {"should_have_pushed": "should_push", "false_garbled_audio": "garbled", "wrong_place": "wrong_place",
               "wrong_category": "wrong_type", "out_of_area": "out_of_area", "late": "late", "rule_to_add": "rule_to_add"}
LEVELS = ("push", "map", "log", "none")
TEXT_MAX = 4000
PLACE_MAX = 200
DONE_MAX = 120
EXPORT_MAX = 5000
FIELDS = ("text", "tags", "corrected_category", "corrected_place", "corrected_level")


class Forbidden(CadError):
    """Not this person's to change (its author or an admin; done: admins): the API's 403, in their activity."""


def _who(operator: str) -> str:
    """"Name <email>" -> "Name" (the note's author as shown)."""
    o = operator or ""
    return o.split(" <", 1)[0].strip() if " <" in o else o


class IntelNotes:
    def __init__(self, cad, engine):
        self.cad, self.engine = cad, engine

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
                "corrected_level": r["corrected_level"], "author": _who(r["author"]), "created_at": r["created_at"],
                "updated_at": r["updated_at"], "edited": r["updated_at"] > r["created_at"], "done": r["done"],
                "done_by": _who(r["done_by"]), "done_at": r["done_at"], "may_edit": mine or admin,
                "review": not r["done"] and any(t in REVIEW_TAGS for t in tags)}

    def of_event(self, eid: int, email: str = "", admin: bool = False) -> dict:
        """GET /api/cad/intel/<id>/notes: the event's notes, oldest first; the audit for an admin; the choices."""
        if self.cad.store.one("SELECT id FROM intel_events WHERE id = ?", (int(eid),)) is None:
            raise CadError(f"no pre-alert {eid}")
        rows = self.cad.store.q("SELECT * FROM intel_notes WHERE event_id = ? ORDER BY created_at, id", (int(eid),))
        out = {"notes": [self.shown(r, email, admin) for r in rows], "tags": list(TAGS), "review_tags": list(REVIEW_TAGS),
               "levels": list(LEVELS), "categories": self.categories(), "admin": admin}
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
        """{event id: {n, tags, review[, done, done_texts]}} for a page of events (the Intel page's chip)."""
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
            if r["done"]:                       # the ✓ on the row: how many, and the first few texts
                o["done"] = o.get("done", 0) + 1
                o.setdefault("done_texts", [])
                if len(o["done_texts"]) < 3:
                    o["done_texts"].append(r["done"])
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
        out = {k: cur[k] for k in FIELDS} if cur else {"text": "", "tags": "[]", "corrected_category": "",
                                                        "corrected_place": "", "corrected_level": ""}
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
        if "corrected_level" in body:
            lv = str(body.get("corrected_level") or "")
            if lv and lv not in LEVELS:
                raise CadError(f"corrected_level: {' | '.join(LEVELS)}")
            out["corrected_level"] = lv
        if not out["text"] and out["tags"] == "[]" and not (out["corrected_category"] or out["corrected_place"]
                                                             or out["corrected_level"]):
            raise CadError("a note needs text, a tag or a correction")
        return out

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

    def _feedback(self, db, note: dict, ev: dict, at: float) -> None:
        """The note's tags as intel_feedback verdicts (the tuning's store), replacing what it wrote before."""
        db.execute("DELETE FROM intel_feedback WHERE note_id = ?", (note["id"],))
        try:
            tags = json.loads(note["tags"] or "[]")
            tids = json.loads(ev["traffic_ids"] or "[]")
        except ValueError:
            tags, tids = [], []
        corrected = {k: note[f"corrected_{k}"] for k in ("place", "level") if note[f"corrected_{k}"]}
        rows = [(TAG_VERDICT[t], None) for t in tags]                      # once per note
        if not tags and (corrected or note["corrected_category"]):
            rows.append((CORRECTED, None))
        if "false_garbled_audio" in tags:
            rows += [(TX_VERDICT, tid) for tid in tids]                      # the clips, labelled per transmission
        for v, tid in rows:
            db.execute("INSERT INTO intel_feedback (event_id, traffic_id, verdict, corrected, corrected_category, rules, "
                       "ruleset_hash, operator, at, note_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (note["event_id"], tid, v, json.dumps(corrected) if corrected else "",
                        note["corrected_category"][:40], ev["rules"] or "[]", ev["ruleset_hash"] or "",
                        note["author"][:120], at, note["id"]))

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
        with self.engine.events._w() as db:
            cur = db.execute("INSERT INTO intel_notes (event_id, text, tags, corrected_category, corrected_place, "
                             "corrected_level, author, author_email, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (int(eid), n["text"], n["tags"], n["corrected_category"], n["corrected_place"],
                              n["corrected_level"], op, email, now, now))
            nid = int(cur.lastrowid)
            note = dict(db.execute("SELECT * FROM intel_notes WHERE id = ?", (nid,)).fetchone())
            self._feedback(db, note, ev, now)
            self._audit(db, nid, int(eid), "add", op, now, None, {k: note[k] for k in FIELDS})
        return {"ok": True, "note": self.shown(note, email, body.get("_may") == "admin")}

    def edit(self, nid: int, body: dict) -> dict:
        op, email = str(body.get("operator") or "")[:160], str(body.get("_email") or "")
        admin = body.get("_may") == "admin"
        now = self.cad.clock()
        with self.engine.events._w() as db:            # read and written in one transaction: never an older copy
            r = db.execute("SELECT * FROM intel_notes WHERE id = ?", (int(nid),)).fetchone()
            if r is None:
                raise CadError(f"no note {nid}")
            cur = dict(r)
            self._may(cur, email, admin)
            e = db.execute("SELECT id, rules, ruleset_hash, traffic_ids FROM intel_events WHERE id = ?",
                           (cur["event_id"],)).fetchone()
            ev = dict(e) if e else {"id": cur["event_id"], "rules": "[]", "ruleset_hash": "", "traffic_ids": "[]"}
            before = {k: cur[k] for k in FIELDS}
            if body.get("delete"):
                db.execute("DELETE FROM intel_notes WHERE id = ?", (int(nid),))
                db.execute("DELETE FROM intel_feedback WHERE note_id = ?", (int(nid),))
                self._audit(db, int(nid), cur["event_id"], "delete", op, now, {**before, "author": cur["author"]}, None)
                return {"ok": True, "deleted": int(nid)}
            n = self._clean(body, cur)
            if n == before:                            # nothing changed: no write, no audit row
                return {"ok": True, "note": self.shown(cur, email, admin), "unchanged": True}
            db.execute("UPDATE intel_notes SET text = ?, tags = ?, corrected_category = ?, corrected_place = ?, "
                       "corrected_level = ?, updated_at = ? WHERE id = ?",
                       (n["text"], n["tags"], n["corrected_category"], n["corrected_place"], n["corrected_level"], now,
                        int(nid)))
            note = dict(db.execute("SELECT * FROM intel_notes WHERE id = ?", (int(nid),)).fetchone())
            self._feedback(db, note, ev, now)
            self._audit(db, int(nid), cur["event_id"], "edit", op, now, before, {k: note[k] for k in FIELDS})
        return {"ok": True, "note": self.shown(note, email, admin)}

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
