"""The intel event store: SqlEvents (cad.sqlite, the live default: Litestream keeps it, the fence guards it) and
MemoryEvents (replay, tests: nothing written anywhere). Both answer the same few calls the engine makes.

Tables (created IF NOT EXISTS here; no SCHEMA_VERSION bump):

    intel_events   one per event (sizeup/intel/model.py Event): no transcript text, ever; traffic_ids are references
    intel_hits     one per (traffic id, rule): every hit, shadow and below-threshold too (recall), 30 days
    intel_feedback a person's verdict on an event (useful | noise | wrong_place | wrong_type | late | missed |
                   should_push: the Intel page's "should have pushed", a held event that was worth a push; garbled |
                   out_of_area | rule_to_add: a note's tags, sizeup/intel/notes.py, note_id its note), a year
    intel_notes    people's notes on an event (sizeup/intel/notes.py): text, tags, corrections, "done / PR #"
    intel_note_audit  every add, edit, delete and done of a note, before and after
    intel_correction_candidates  misheard -> real pairs from notes' corrected places, for the speech-to-text side
                   (sizeup/intel/notes.py correction_candidates, #956)

Events are kept 90 days (the radio log's own default for context traffic), hits 30; an event with a note is kept as
long as its notes (a year: KEEP_NOTES_S), so the notes stay usable for tuning.

intel_events.decided: rules | model | both | "" (decided_of: who raised it, the Intel page's filter), written by save();
added by ALTER TABLE at start, and a row without it (an older release's) filled in then.

intel_events.delivery: the last push's outcome at ntfy, JSON {ok, at, status, id} or {ok: false, at, status, error} ("":
none yet), written by delivered() from the notify thread (sizeup/notify.py Notifier.send's done). Not an Event field:
the engine's save() never touches it, so a save racing the notify thread can't undo it. Added to an older table by
ALTER TABLE at start.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import fields

from sizeup.intel.model import Event

KEEP_EVENTS_S = 90 * 86400
KEEP_HITS_S = 30 * 86400
KEEP_FEEDBACK_S = 366 * 86400
KEEP_NOTES_S = 366 * 86400
VERDICTS = ("useful", "noise", "wrong_place", "wrong_type", "late", "missed", "should_push", "garbled", "out_of_area",
            "rule_to_add")
MODEL_PREFIXES = ("model.", "infer.")      # a rule of the model's (backing.py, infer.py), not of a pack


def decided_of(rules) -> str:
    """rules | model | both | "" (none): who raised it, from its rules list (intel_events.decided, the Intel page)."""
    ids = [str(r[0]) for r in rules or [] if r]
    m = any(i.startswith(MODEL_PREFIXES) for i in ids)
    o = any(not i.startswith(MODEL_PREFIXES) for i in ids)
    return "both" if m and o else "model" if m else "rules" if o else ""


BACKFILL_CHUNK = 5000              # intel_events.decided filled in at start: rows a transaction

# a person's votes (vote()): one of each group per person and event; the same one again takes it back
VOTE_PAIRS = {"useful": ("useful", "noise"), "noise": ("useful", "noise"), "should_push": ("should_push",)}
VOTE_ROWS = "traffic_id IS NULL AND note_id IS NULL"     # (a vote is on the event: never one transmission,
                                                         # never a note's tag, sizeup/intel/notes.py)

SCHEMA = """
CREATE TABLE IF NOT EXISTS intel_events (
  id INTEGER PRIMARY KEY,
  domain TEXT NOT NULL, pack TEXT NOT NULL, category TEXT NOT NULL, subject TEXT NOT NULL,
  source_key TEXT NOT NULL DEFAULT '', system TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
  dept TEXT NOT NULL DEFAULT '',
  first_at REAL NOT NULL, last_at REAL NOT NULL, until REAL NOT NULL,
  status TEXT NOT NULL,                              -- open | ended | expired | dismissed
  what_score REAL NOT NULL DEFAULT 0, level TEXT NOT NULL DEFAULT '',
  rules TEXT NOT NULL DEFAULT '[]',                  -- [[rule id, rev, weight]]
  ruleset_hash TEXT NOT NULL DEFAULT '',
  where_score REAL NOT NULL DEFAULT 0, precision TEXT NOT NULL DEFAULT 'none',
  lat REAL, lon REAL, radius_m REAL NOT NULL DEFAULT 0,
  label_full TEXT NOT NULL DEFAULT '', town TEXT NOT NULL DEFAULT '', alts TEXT NOT NULL DEFAULT '[]',
  basis TEXT NOT NULL DEFAULT '', confirmed INTEGER NOT NULL DEFAULT 0,
  traffic_ids TEXT NOT NULL DEFAULT '[]',
  mode TEXT NOT NULL DEFAULT 'log', priority INTEGER NOT NULL DEFAULT 3,
  pushed_at REAL, push_count INTEGER NOT NULL DEFAULT 0, suppressed TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  near_incident INTEGER, incident_id INTEGER,
  fd TEXT NOT NULL DEFAULT '',                       -- the FD at a fire-related event's place: on_scene | dispatched | there | cleared
  updated_at REAL NOT NULL,
  dismissed_by TEXT NOT NULL DEFAULT '',
  delivery TEXT NOT NULL DEFAULT ''                  -- the last push's outcome at ntfy (JSON; the module doc)
);
CREATE INDEX IF NOT EXISTS intel_events_status ON intel_events(status, until);
CREATE INDEX IF NOT EXISTS intel_events_source ON intel_events(source_key, last_at);
CREATE INDEX IF NOT EXISTS intel_events_cat ON intel_events(domain, category, first_at);
CREATE INDEX IF NOT EXISTS intel_events_first ON intel_events(first_at);   -- the Intel page (eventlog.py): newest first
CREATE TABLE IF NOT EXISTS intel_hits (
  traffic_id TEXT NOT NULL, rule_id TEXT NOT NULL, rule_rev INTEGER NOT NULL, weight REAL NOT NULL,
  category TEXT NOT NULL, pack TEXT NOT NULL, event_id INTEGER, at REAL NOT NULL,
  UNIQUE(traffic_id, rule_id)
);
CREATE INDEX IF NOT EXISTS intel_hits_at ON intel_hits(at);
CREATE INDEX IF NOT EXISTS intel_hits_rule ON intel_hits(rule_id, at);
CREATE TABLE IF NOT EXISTS intel_feedback (
  id INTEGER PRIMARY KEY,
  event_id INTEGER, traffic_id TEXT,
  verdict TEXT NOT NULL,
  corrected TEXT NOT NULL DEFAULT '',                -- {lat, lon, precision} when a marker is dragged (PR 5)
  corrected_category TEXT NOT NULL DEFAULT '',
  rules TEXT NOT NULL DEFAULT '[]', ruleset_hash TEXT NOT NULL DEFAULT '',
  operator TEXT NOT NULL DEFAULT '', at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS intel_feedback_event ON intel_feedback(event_id);
CREATE TABLE IF NOT EXISTS intel_notes (
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]',                   -- notes.TAGS
  corrected_category TEXT NOT NULL DEFAULT '', corrected_place TEXT NOT NULL DEFAULT '',
  corrected_level TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT '', author_email TEXT NOT NULL DEFAULT '',
  corrected_lat REAL, corrected_lon REAL,            -- the corrected place picked (notes.py PLACE_COLS; NULL: none,
  corrected_precision TEXT NOT NULL DEFAULT '',      -- or an older note's free text alone, corrected_place)
  corrected_label TEXT NOT NULL DEFAULT '', corrected_qualifier TEXT NOT NULL DEFAULT '',
  corrected_place_key TEXT NOT NULL DEFAULT '',      -- a common place's key (settings_overrides "places")
  heard TEXT NOT NULL DEFAULT '',                    -- the words heard for it ("Sampel Diner")
  created_at REAL NOT NULL, updated_at REAL NOT NULL,
  done TEXT NOT NULL DEFAULT '', done_by TEXT NOT NULL DEFAULT '', done_at REAL   -- acted on: "PR #123", "rule added"
);
CREATE INDEX IF NOT EXISTS intel_notes_event ON intel_notes(event_id);
CREATE INDEX IF NOT EXISTS intel_notes_created ON intel_notes(created_at);
CREATE TABLE IF NOT EXISTS intel_note_audit (
  id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
  action TEXT NOT NULL,                              -- add | edit | delete | done
  operator TEXT NOT NULL DEFAULT '', at REAL NOT NULL,
  before TEXT NOT NULL DEFAULT '', after TEXT NOT NULL DEFAULT ''     -- the note as JSON ('' none)
);
CREATE INDEX IF NOT EXISTS intel_note_audit_note ON intel_note_audit(note_id);
CREATE TABLE IF NOT EXISTS intel_correction_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER,      -- (never reused: mark_imported names what it read)
  clip_id TEXT NOT NULL DEFAULT '',                  -- the transmission (radio_traffic id) the words were heard in
  heard_phrase TEXT NOT NULL, real_phrase TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'other',                -- place | unit | street | other
  source TEXT NOT NULL DEFAULT '',                   -- intel-note
  who TEXT NOT NULL DEFAULT '', "when" REAL NOT NULL,
  imported_at REAL                                   -- NULL: the speech-to-text side hasn't taken it yet
);
CREATE INDEX IF NOT EXISTS intel_correction_candidates_when ON intel_correction_candidates("when");
CREATE INDEX IF NOT EXISTS intel_correction_candidates_note ON intel_correction_candidates(note_id);
"""

JSON_COLS = ("rules", "alts", "traffic_ids")
# intel_notes' picked place (sizeup/intel/notes.py), added to an older table at start
NOTE_PLACE_COLS = (("corrected_lat", "REAL"), ("corrected_lon", "REAL"),
                   ("corrected_precision", "TEXT NOT NULL DEFAULT ''"), ("corrected_label", "TEXT NOT NULL DEFAULT ''"),
                   ("corrected_qualifier", "TEXT NOT NULL DEFAULT ''"), ("corrected_place_key", "TEXT NOT NULL DEFAULT ''"),
                   ("heard", "TEXT NOT NULL DEFAULT ''"))
EVENT_COLS = tuple(f.name for f in fields(Event) if f.name != "id")


def row_event(r: dict) -> Event:
    d = {k: r[k] for k in EVENT_COLS if k in r}
    for k in JSON_COLS:
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except ValueError:
            d[k] = []
    d["confirmed"] = bool(d.get("confirmed"))
    return Event(id=r["id"], **d)


def _delivery(raw) -> dict | None:
    try:
        d = json.loads(raw) if raw else None
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


class MemoryEvents:
    """Events in memory only (replay, tests)."""

    def __init__(self):
        self.rows: dict[int, Event] = {}
        self.delivery: dict[int, dict] = {}
        self._next = 1
        self._lock = threading.Lock()

    def open_events(self) -> list[Event]:
        with self._lock:
            return [e for e in self.rows.values() if e.status == "open"]

    def get(self, eid: int) -> Event | None:
        return self.rows.get(eid)

    def save(self, ev: Event) -> Event:
        with self._lock:
            if ev.id is None:
                ev.id = self._next
                self._next += 1
            self.rows[ev.id] = ev
        return ev

    def all(self) -> list[Event]:
        return sorted(self.rows.values(), key=lambda e: e.id or 0)

    def bound(self, tid: str, pack: str, category: str) -> bool:
        """This row is already one of an event's of this pack and category (SqlEvents.bound; never one withdrawn)."""
        with self._lock:
            return any(e.pack == pack and e.category == category and tid in e.traffic_ids
                       and e.note != "source withdrawn" for e in self.rows.values())

    def delivered(self, eid: int, outcome: dict) -> None:
        self.delivery[int(eid)] = dict(outcome)

    def recent_closed(self, pack: str, category: str, since: float) -> list[Event]:
        return [e for e in self.rows.values() if e.status in ("ended", "dismissed") and e.pack == pack
                and e.category == category and e.updated_at >= since]


class SqlEvents:
    """Events in cad.sqlite (the live store)."""

    def __init__(self, store):
        self.store = store
        self.rev = 0                       # bumped on every intel write: the console's intel card reads by it
        self.list_rev = 0                  # ... but a rule hit: what the Intel page lists (eventlog.py) changed
        # ... an event saved or dismissed: what its map shows (#937; never a vote). From the start time in ms, so a
        # page open across a hub restart never sees a value it already had
        self.map_rev = int(time.time() * 1000)
        with store.lock:
            store.db.executescript(SCHEMA)
            cols = {r[1] for r in store.db.execute("PRAGMA table_info(intel_events)")}
            if "delivery" not in cols:              # a table from before the column
                store.db.execute("ALTER TABLE intel_events ADD COLUMN delivery TEXT NOT NULL DEFAULT ''")
            if "fd" not in cols:                    # a table from before the FD check (engine._fire)
                store.db.execute("ALTER TABLE intel_events ADD COLUMN fd TEXT NOT NULL DEFAULT ''")
            ncols = {r[1] for r in store.db.execute("PRAGMA table_info(intel_notes)")}
            for col, decl in NOTE_PLACE_COLS:       # a table from before the picked place: added, never dropped
                if col not in ncols:
                    store.db.execute(f"ALTER TABLE intel_notes ADD COLUMN {col} {decl}")
            fcols = {r[1] for r in store.db.execute("PRAGMA table_info(intel_feedback)")}
            if "note_id" not in fcols:              # a note's tags as verdicts (sizeup/intel/notes.py)
                store.db.execute("ALTER TABLE intel_feedback ADD COLUMN note_id INTEGER")
                store.db.execute("CREATE INDEX IF NOT EXISTS intel_feedback_note ON intel_feedback(note_id)")
            if "decided" not in cols:               # rules | model | both | '' (decided_of): the Intel page's filter
                store.db.execute("ALTER TABLE intel_events ADD COLUMN decided TEXT")     # (eventlog.py); NULL: an older
            todo = store.db.execute("SELECT id, rules FROM intel_events WHERE decided IS NULL LIMIT 200000").fetchall() \
                if "rules" in cols else []          # release's row, read from its text until the next start fills it in
            if todo:                                # (once: the rows from before the column, or an older release's)
                def _d(raw):
                    try:
                        return decided_of(json.loads(raw or "[]"))
                    except ValueError:
                        return ""
                for k in range(0, len(todo), BACKFILL_CHUNK):     # a transaction a chunk: never a commit a row
                    part = todo[k:k + BACKFILL_CHUNK]
                    store.db.execute("BEGIN IMMEDIATE")
                    try:
                        store.db.executemany("UPDATE intel_events SET decided = ? WHERE id = ?",
                                             [(_d(r[1]), r[0]) for r in part])
                    except BaseException:
                        store.db.execute("ROLLBACK")
                        raise
                    store.db.execute("COMMIT")

    @contextmanager
    def _w(self, listed: bool = True, placed: bool = False):
        """One transaction under the store's lock WITHOUT store.rev (#492 review, H4: intel writes are not CAD changes;
        a police transmission must not rebuild every console's state), the way sizeup/cad/activity.py writes."""
        with self.store.lock:
            db = self.store.db
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
            self.rev += 1
            if listed:
                self.list_rev += 1
            if placed:
                self.map_rev += 1

    def recent_closed(self, pack: str, category: str, since: float) -> list[Event]:
        """The events of this pack and category ended or dismissed since then (the mute compares their places)."""
        return [row_event(r) for r in self.store.q(
            "SELECT * FROM intel_events WHERE status IN ('ended', 'dismissed') AND pack = ? AND category = ? AND "
            "updated_at >= ? LIMIT 50", (pack, category, since))]

    def open_events(self) -> list[Event]:
        return [row_event(r) for r in self.store.q("SELECT * FROM intel_events WHERE status = 'open'")]

    def bound(self, tid: str, pack: str, category: str) -> bool:
        """This row already has a hit of this pack and category bound to an event (a re-feed after a restart: never a
        second event from it)."""
        return self.store.one("SELECT 1 FROM intel_hits WHERE traffic_id = ? AND pack = ? AND category = ? AND "
                              "event_id IS NOT NULL LIMIT 1", (tid, pack, category)) is not None

    def get(self, eid: int) -> Event | None:
        r = self.store.one("SELECT * FROM intel_events WHERE id = ?", (eid,))
        return row_event(r) if r else None

    def save(self, ev: Event) -> Event:
        vals = []
        for k in EVENT_COLS:
            v = getattr(ev, k)
            vals.append(json.dumps(v) if k in JSON_COLS else int(v) if isinstance(v, bool) else v)
        cols = (*EVENT_COLS, "decided")                 # (+ who raised it: the Intel page's filter, decided_of)
        vals.append(decided_of(ev.rules))
        with self._w(placed=True) as db:
            if ev.id is None:
                cur = db.execute(f"INSERT INTO intel_events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
                ev.id = int(cur.lastrowid)
            else:
                db.execute(f"UPDATE intel_events SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?", (*vals, ev.id))
        return ev

    def rows(self, where: str = "1", args=(), limit: int = 200) -> list[dict]:
        out = []
        for r in self.store.q(f"SELECT * FROM intel_events WHERE {where} ORDER BY last_at DESC, id DESC LIMIT ?", (*args, limit)):
            e = row_event(r).as_dict()
            e["dismissed_by"] = r.get("dismissed_by") or ""
            e["delivery"] = _delivery(r.get("delivery"))
            out.append(e)
        return out

    def delivered(self, eid: int, outcome: dict) -> None:
        """The last push's outcome at ntfy (the notify thread): that column only."""
        keep = {k: outcome[k] for k in ("ok", "at", "status", "id", "error") if k in outcome}
        with self._w() as db:
            db.execute("UPDATE intel_events SET delivery = ? WHERE id = ?", (json.dumps(keep), int(eid)))

    def dismiss(self, eid: int, operator: str, now: float) -> bool:
        with self._w(placed=True) as db:
            return bool(db.execute("UPDATE intel_events SET status = 'dismissed', dismissed_by = ?, updated_at = ? "
                                   "WHERE id = ? AND status = 'open'", (operator[:120], now, eid)).rowcount)

    def feedback(self, eid: int | None, tid: str | None, verdict: str, operator: str, now: float,
                 corrected: dict | None = None, corrected_category: str = "") -> int:
        ev = self.get(eid) if eid is not None else None
        with self._w() as db:
            cur = db.execute("INSERT INTO intel_feedback (event_id, traffic_id, verdict, corrected, corrected_category, rules, "
                             "ruleset_hash, operator, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (eid, tid, verdict, json.dumps(corrected) if corrected else "", corrected_category[:40],
                              json.dumps(ev.rules if ev else []), ev.ruleset_hash if ev else "", operator[:120], now))
            return int(cur.lastrowid)

    def ensure_vote(self, eid: int, verdict: str, operator: str, now: float) -> int | None:
        """This person's vote is this one (a Dismiss is a Noise vote): set when it isn't, never taken back."""
        return self.vote(eid, verdict, operator, now, toggle=False)[0]

    def vote(self, eid: int, verdict: str, operator: str, now: float, toggle: bool = True) -> tuple[int | None, bool]:
        """A person's vote on an event (the Intel page's and the card's buttons; #906 review M1), one each: Useful and
        Noise exclusive (the other one replaced), Should have pushed on its own; the same one again takes it back.
        (id of the vote, or None; True when it was taken back). By operator: who signed in (the API sets it)."""
        ev = self.get(eid)
        pair = VOTE_PAIRS.get(verdict, (verdict,))
        op = operator[:120]
        marks = ",".join("?" * len(pair))
        with self._w() as db:
            had = {r[0]: r[1] for r in db.execute(f"SELECT verdict, id FROM intel_feedback WHERE event_id = ? AND operator = ? "
                                                  f"AND verdict IN ({marks}) AND {VOTE_ROWS}", (eid, op, *pair))}
            if not toggle and verdict in had:
                return int(had[verdict]), False
            db.execute(f"DELETE FROM intel_feedback WHERE event_id = ? AND operator = ? AND verdict IN ({marks}) AND {VOTE_ROWS}",
                       (eid, op, *pair))
            if verdict in had:
                return None, True
            cur = db.execute("INSERT INTO intel_feedback (event_id, traffic_id, verdict, corrected, corrected_category, rules, "
                             "ruleset_hash, operator, at) VALUES (?, NULL, ?, '', '', ?, ?, ?, ?)",
                             (eid, verdict, json.dumps(ev.rules if ev else []), ev.ruleset_hash if ev else "", op, now))
            return int(cur.lastrowid), False

    def hit(self, h) -> None:
        with self._w(listed=False) as db:
            db.execute("INSERT INTO intel_hits (traffic_id, rule_id, rule_rev, weight, category, pack, event_id, at) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(traffic_id, rule_id) DO UPDATE SET rule_rev = excluded.rule_rev, "
                       "weight = excluded.weight, category = excluded.category, pack = excluded.pack, "
                       "event_id = COALESCE(excluded.event_id, intel_hits.event_id), at = excluded.at",
                       (h.traffic_id, h.rule_id, h.rule_rev, h.weight, h.category, h.pack, h.event_id, h.at))

    def clear_hits(self, tid: str, keep: set[str]) -> None:
        """A re-transcription: the rules that no longer hit this transmission go (idempotent per traffic id)."""
        with self._w(listed=False) as db:
            if keep:
                db.execute(f"DELETE FROM intel_hits WHERE traffic_id = ? AND rule_id NOT IN ({','.join('?' * len(keep))})",
                           (tid, *sorted(keep)))
            else:
                db.execute("DELETE FROM intel_hits WHERE traffic_id = ?", (tid,))

    def stats(self, since: float) -> dict:
        """{rule id: {hits, events}} since then (Settings > Intel's table)."""
        out: dict = {}
        for r in self.store.q("SELECT rule_id, COUNT(*) AS hits, COUNT(DISTINCT event_id) AS events FROM intel_hits "
                              "WHERE at >= ? GROUP BY rule_id", (since,)):
            out[r["rule_id"]] = {"hits": r["hits"], "events": r["events"]}
        return out

    def counts(self, since: float) -> dict:
        r = self.store.one("SELECT COUNT(*) AS events, SUM(pushed_at IS NOT NULL) AS pushed, "
                           "SUM(suppressed != '') AS suppressed FROM intel_events WHERE first_at >= ?", (since,)) or {}
        return {"events": r.get("events") or 0, "pushed": r.get("pushed") or 0, "suppressed": r.get("suppressed") or 0}

    def prune(self, now: float) -> int:
        with self._w() as db:
            n = db.execute("DELETE FROM intel_notes WHERE updated_at < ?", (now - KEEP_NOTES_S,)).rowcount
            n += db.execute("DELETE FROM intel_note_audit WHERE at < ?", (now - KEEP_NOTES_S,)).rowcount
            n += db.execute('DELETE FROM intel_correction_candidates WHERE "when" < ?', (now - KEEP_NOTES_S,)).rowcount
            n += db.execute("DELETE FROM intel_events WHERE last_at < ? AND status != 'open' AND id NOT IN "
                            "(SELECT event_id FROM intel_notes)", (now - KEEP_EVENTS_S,)).rowcount   # (a note keeps it)
            n += db.execute("DELETE FROM intel_hits WHERE at < ?", (now - KEEP_HITS_S,)).rowcount
            n += db.execute("DELETE FROM intel_feedback WHERE at < ?", (now - KEEP_FEEDBACK_S,)).rowcount
        return n
