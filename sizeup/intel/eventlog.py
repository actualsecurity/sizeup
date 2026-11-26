"""The Intel page (static/intel-page.js, /cad#intel): every radio intel event at every level and status, pushed or not,
and why one wasn't pushed. Admin decision, 2026-11-25: "a page in the app where we can view these intel events even if
they do not trigger". Anyone signed in reads it, as the console's intel card (sourcekeys.may_see_context, the API's
gate); names and PII as the events have them; nothing here leaves the hub.

    GET /api/cad/intel/list?from=&to=&level=&category=&source=&system=&decided=&held=&status=&q=&tag=&review=&page=&size=
        from / to: a date (2026-11-01, the CAD's time zone; "to" a whole day) or unix seconds; none: the last 24 h (the
        review queue: every event kept). q: words in its notes; tag: a note's tag; review=1: a note tagged for a rule
        or model change (notes.REVIEW_TAGS) not yet done (sizeup/intel/notes.py).
        {events: [row], total, page, size, since, until, facets: {categories, sources, systems}, held: [{key, label,
        tip}]}. Newest first (first_at, then id), PAGE_SIZE a page (at most PAGE_MAX), on intel_events_first.

A row (terse; the page shows it as is):

    id, at (first_at), last_at, status, pack, pack_label, category, label, level, would, what, score_level, parts
    ([[rule id, weight]]), decided, source_key, source (its name: source_name()), system, place, precision, confirmed,
    town, held ({key, label, detail} | None when pushed), pushed_at, n (transmissions), votes ({verdict: n}), mine
    (this reader's verdicts), near_incident, incident_id

level      what happened: push (pushed_at set), held (at push, not pushed: the policy's or the sink's reason), else
           its mode: map, log, shadow
would      the level it would have had if not held ("" when the same or unknown): an engine hold (held at log: out of
           area, unplaced, the FD there ...) gives back the rung its rules reach (engine._event_mode; the model's own
           event: its pack's and category's lowest); a model event capped at map, the same
decided    rules | model | both: its rules list, model.* / infer.* (the model behind the rules, backing.py; the LLM
           stage, infer.py) against every other rule (cues and dampers too)
held       why it didn't push: HELD_SQL, one CASE over the row (the same expression filters ?held=, so a filtered page
           is what its rows say). Event.suppressed first (every value the engine and the policy write: SUPPRESSED),
           else from its mode and status by today's settings (DERIVED): the pack's or the category's mode, the
           model's cap (uncorroborated: no dispatch cue or rule word of its category and no confirmed place, backing.py
           _corroborated), the rules' own rung, shadow; at push with no reason, its status (dismissed, expired, ended)

Source names come from one helper, source_name() (the radio log's channel per source key, comms.sources_seen: the
Radio page's own), so a Settings > Radios name resolver can replace it in one place.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta

from sizeup.cad.store import TZ, CadError
from sizeup.intel.events import MODEL_PREFIXES, VOTE_ROWS, decided_of
from sizeup.intel.model import lowest, mode_rank

PAGE_SIZE = 50
PAGE_MAX = 100
DEFAULT_S = 24 * 3600
FACET_S = 300                      # the filters' choices: read again when the range moved this far (or the events changed)
SPAN_MAX_S = 92 * 86400            # events are kept 90 days (events.py KEEP_EVENTS_S)
NOTES_SPAN_S = 367 * 86400         # ... and a year with a note (KEEP_NOTES_S): the review queue's default range
LEVELS = ("push", "held", "map", "log", "shadow")
DECIDED = ("rules", "model", "both")
STATUSES = ("open", "ended", "expired", "dismissed")

# Event.suppressed: every value the engine (_hold, _hw_hold, _fire, _after) and the policy (decide) write, its short
# label and its hover. A value not here (a later engine's) shows as itself.
SUPPRESSED = {
    "unplaced": ("unplaced", "No place a crew can drive to yet (a category that needs one; a highway crash with no "
                             "exit, marker or route)"),
    "out_of_area": ("out of area", "Its place, or the towns its words name, outside the source's towns, the pack's area "
                                   "or (a highway crash) our district's stretch"),
    "near_incident": ("our call", "At one of our own open incidents: the job we already have, linked"),
    "placed_late": ("placed late", "Its place came more than 5 min after the call-out: too late for a pre-alert"),
    "fd_on_scene": ("FD on scene", "A fire-related call the FD was already on scene at"),
    "fd_dispatched": ("FD dispatched", "A fire-related call the FD was already dispatched to"),
    "fd_there": ("FD there", "The words said the FD was already there"),
    "fd_late": ("FD late", "Held for the FD, and that incident gone more than 5 min after the call: too late"),
    "shadow": ("shadow", "Counted only: its pack, category or rule is in shadow"),
    "off": ("pushes off", "The kill switch (Settings > System > Intel: Push) was off"),
    "quiet": ("quiet hours", "In quiet hours (Settings > System > Intel)"),
    "rate": ("cap", "Over a push cap (an hour, a day, a category an hour): in the digest instead"),
    "merged": ("merged", "An update within the repush limits of an earlier push"),
    "scrub": ("scrubbed", "Withheld by the PII scrub (PII off)"),
    "unsent": ("unsent", "Not taken for sending: no pre-alert topic in effect, the queue full, or not the leader"),
    "late": ("late", "Its transcripts reached intel too late (over the pack's late s after the clip ended, a catch-up "
                     "or an outage): kept as context and at log, never pushed"),
}
# derived when Event.suppressed is empty (HELD_SQL): label, hover
DERIVED = {
    "mode_pack": ("pack", "Its pack's mode (Settings > System > Intel) is below push"),
    "mode_category": ("category", "Its category's mode (Settings > System > Intel) is below push"),
    "mode_rule": ("rules", "Its rules at push mode alone don't reach the threshold (a rule at a lower mode adds to the "
                           "score, never to the level)"),
    "uncorroborated": ("uncorroborated", "The model's alone: no dispatch cue or rule word of its category and no "
                                         "confirmed place in its transmission, so capped at map (Corroborate on)"),
    "model_cap": ("model cap", "The model's: capped below push (its mode, or another call open on the source)"),
    "dismissed": ("dismissed", "Dismissed before it pushed"),
    "expired": ("expired", "Expired before it pushed"),
    "ended": ("ended", "Ended (a disregard) before it pushed"),
    "not_pushed": ("not pushed", "At push, not pushed (yet)"),
}
HELD = {**SUPPRESSED, **DERIVED}
ENGINE_HOLDS = ("unplaced", "out_of_area", "near_incident", "placed_late", "late", "fd_on_scene", "fd_dispatched", "fd_there",
                "fd_late")
POLICY_HOLDS = ("off", "quiet", "rate", "merged", "scrub", "unsent")
HELD_KEY = re.compile(r"[a-z][a-z_]{0,39}")

_IS_MODEL = " OR ".join(f"json_extract(j.value, '$[0]') LIKE '{p}%'" for p in MODEL_PREFIXES)
# The rules list as stored (json.dumps of [[id, rev, weight], ...]: each rule's id right after '["'), read by its text:
# string functions over the row, never json_each per row (a 90-day filtered view, #906 review M2). Python's twin:
# decided_by().
_N_RULES = "((length(e.rules) - length(replace(e.rules, '[\"', ''))) / 2)"
_N_MODEL = ("((length(e.rules) - length(" + "".join("replace(" for _ in MODEL_PREFIXES) + "e.rules"
            + "".join(f", '[\"{p}', '')" for p in MODEL_PREFIXES) + f")) / {len(MODEL_PREFIXES[0]) + 2})")
assert len({len(p) for p in MODEL_PREFIXES}) == 1          # (one length: the count's divisor)
HAS_MODEL = "(" + " OR ".join(f"instr(e.rules, '[\"{p}') > 0" for p in MODEL_PREFIXES) + ")"
HAS_RULE = f"({_N_RULES} > {_N_MODEL})"
# only for a model event at map (the CASE reaches it for those rows alone)
HAS_POS_RULE = f"EXISTS (SELECT 1 FROM json_each(e.rules) j WHERE NOT ({_IS_MODEL}) AND json_extract(j.value, '$[2]') > 0)"
# who raised it: the column save() writes (events.decided_of), else (a row an older release wrote) from its text
DECIDED_SQL = (f"COALESCE(e.decided, CASE WHEN {HAS_MODEL} AND {HAS_RULE} THEN 'both' WHEN {HAS_MODEL} THEN 'model' "
               f"WHEN {HAS_RULE} THEN 'rules' ELSE '' END)")
IS_MODEL_ROW = f"(e.decided IN ('model', 'both') OR (e.decided IS NULL AND {HAS_MODEL}))"
# why it didn't push (the module doc). :packs / :cats: today's pack and category modes, as "pack|mode" and
# "pack|category|mode" (classes()); :corr: Corroborate (backing.py infer_pd_corroborate)
HELD_SQL = f"""CASE
  WHEN e.pushed_at IS NOT NULL THEN ''
  WHEN e.suppressed != '' THEN e.suppressed
  WHEN e.status IN ('dismissed', 'expired', 'ended') THEN e.status
  WHEN e.mode IN ('log', 'map') AND (e.pack || '|' || e.mode) IN (SELECT value FROM json_each(:packs)) THEN 'mode_pack'
  WHEN e.mode IN ('log', 'map') AND (e.pack || '|' || e.category || '|' || e.mode) IN (SELECT value FROM json_each(:cats))
    THEN 'mode_category'
  WHEN e.mode IN ('log', 'map') AND {IS_MODEL_ROW} THEN
    CASE WHEN e.mode = 'map' AND :corr AND e.confirmed = 0 AND NOT {HAS_POS_RULE} THEN 'uncorroborated' ELSE 'model_cap' END
  WHEN e.mode IN ('log', 'map') THEN 'mode_rule'
  WHEN e.mode = 'shadow' THEN 'shadow'
  ELSE 'not_pushed'
END"""
LEVEL_SQL = {"push": "pushed_at IS NOT NULL", "held": "pushed_at IS NULL AND mode = 'push'",     # (on the listing's
             "map": "pushed_at IS NULL AND mode = 'map'", "log": "pushed_at IS NULL AND mode = 'log'",  # own columns)
             "shadow": "pushed_at IS NULL AND mode = 'shadow'"}
COLS = ("id, pack, category, source_key, system, first_at, last_at, status, what_score, level, rules, precision, "
        "label_full, town, confirmed, mode, pushed_at, suppressed, near_incident, incident_id, traffic_ids")


def is_model(rule_id) -> bool:
    return str(rule_id).startswith(MODEL_PREFIXES)


def decided_by(rules) -> str:
    """rules | model | both | "" (none): the rules list's model.* / infer.* against the rest (DECIDED_SQL's twin)."""
    return decided_of(rules)


def held_label(key: str, mode: str = "") -> tuple[str, str]:
    """(label, hover) of a held key; a mode key says the mode ("pack log", "category map")."""
    label, tip = HELD.get(key, (key.replace("_", " "), ""))
    if key in ("mode_pack", "mode_category", "mode_rule") and mode:
        label = f"{label} {mode}"
    return label, tip


def held_options() -> list[dict]:
    return [{"key": k, "label": v[0], "tip": v[1]} for k, v in HELD.items()]


def source_names(cad) -> dict[str, str]:
    """{source key: its name}: the radio log's channel per source (comms.sources_seen, once per rev: the Radio page's
    own names). The one place the Intel page names a source."""
    try:
        return {r["source"]: r["channel"] for r in cad.comms.sources_seen() if r.get("source") and r.get("channel")}
    except Exception:  # noqa: BLE001 - a name is never worth the page
        return {}


def source_name(names: dict, key: str) -> str:
    """A source's name: the resolver's (source_names), else the key's own words ("mrrs 9211", "FG1")."""
    if not key:
        return ""
    if names.get(key):
        return names[key]
    parts = key.split(":")
    if parts[0] == "p25" and len(parts) == 3:
        return f"{parts[1]} {parts[2]}"
    if parts[0] == "tg" and len(parts) == 2:
        return f"TG {parts[1]}"
    return parts[-1]


def _when(v: str, end: bool) -> float | None:
    v = (v or "").strip()[:40]
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        f = None
    if f is not None:
        if not math.isfinite(f):
            raise CadError("from, to: a date (2026-11-01) or unix seconds")
        return f
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        raise CadError("from, to: a date (2026-11-01) or unix seconds") from None
    if end and len(v) <= 10:           # a day: to its end, the next local midnight (a DST day is 23 or 25 h)
        d = datetime.combine(d.date() + timedelta(days=1), datetime.min.time())
    if d.tzinfo is None:
        d = d.replace(tzinfo=TZ)
    return d.timestamp()


class IntelLog:
    """The Intel page's list (the module doc): one read per page, nothing written here."""

    def __init__(self, cad, engine):
        self.cad, self.engine = cad, engine
        self._facets: tuple | None = None          # (key, facets): the range's choices, again only on a new revision
        self._totals: dict = {}                     # filters -> (revision, total): a poll's count, again only then

    def _rev(self) -> int:
        """The events' revision for what the list shows (events.py list_rev: an event saved, dismissed, voted on or
        delivered; never a rule hit)."""
        return int(getattr(self.engine.events, "list_rev", getattr(self.engine.events, "rev", 0)))

    # -- today's settings, as HELD_SQL reads them ----------------------------------------------------------------------

    def classes(self) -> dict:
        """{packs, cats, corr}: the pack and category modes below push (HELD_SQL's :packs / :cats) and Corroborate."""
        packs, cats = [], []
        for p in getattr(self.engine, "packs", []) or []:
            for m in ("log", "map"):
                if p.mode == m:
                    packs.append(f"{p.name}|{m}")
                for c, d in p.categories.items():
                    if d.get("mode") == m and p.mode != m:
                        cats.append(f"{p.name}|{c}|{m}")
        corr = True
        b = getattr(self.engine, "backing", None)
        if b is not None:
            try:
                corr = bool(b.cfg().get("infer_pd_corroborate", True))
            except Exception:  # noqa: BLE001 - its default
                corr = True
        return {"packs": json.dumps(packs), "cats": json.dumps(cats), "corr": int(corr)}

    def would(self, r: dict, key: str, decided: str) -> str:
        """The level it would have had if not held (the module doc); "" when the same or not known."""
        if r["pushed_at"] is not None:
            return ""
        p = self.engine.pack(r["pack"]) if hasattr(self.engine, "pack") else None
        if key in POLICY_HOLDS:
            return "push"
        if key in ("mode_pack", "mode_category"):          # held by a mode: its rules' own rung is push (#906 review)
            return "push" if mode_rank(r["mode"]) < mode_rank("push") else ""
        if p is None:
            return ""
        cap = lowest(p.mode, p.category(r["category"]).get("mode", "push"))
        if key in ("uncorroborated", "model_cap"):
            out = cap
        elif key in ENGINE_HOLDS:
            if decided == "model":
                out = cap
            else:
                from sizeup.intel.model import Event
                try:
                    out = self.engine._event_mode(p, Event(pack=r["pack"], category=r["category"], rules=r["rules"]))
                except Exception:  # noqa: BLE001 - not known
                    return ""
        else:
            return ""
        return out if mode_rank(out) > mode_rank(r["mode"]) else ""

    # -- the page ------------------------------------------------------------------------------------------------------

    def query(self, q: dict) -> dict:
        """The filters of a query string as SQL: {inner, outer, args, since, until, page, size} (list(), ids())."""
        def one(k: str, n: int = 80) -> str:
            return str((q.get(k) or [""])[0] or "").strip()[:n]

        def many(k: str, allowed) -> list[str]:
            vals = [x for x in one(k, 200).split(",") if x]
            bad = [x for x in vals if x not in allowed]
            if bad:
                raise CadError(f"{k}: {' | '.join(allowed)}")
            return vals
        from sizeup.intel.notes import REVIEW_TAGS, TAGS
        now = self.cad.clock()
        review = one("review", 4) in ("1", "true", "yes")
        # by the notes (their words, a tag): every event kept, a noted one up to a year (#913 review F2)
        noted = review or bool(one("q", 200)) or bool(one("tag", 200))
        span = NOTES_SPAN_S if noted else SPAN_MAX_S
        until = _when(one("to", 40), True)
        since = _when(one("from", 40), False)
        until = now + 60 if until is None else until
        if since is None:
            since = until - (span if noted else DEFAULT_S) if one("to") or noted else now - DEFAULT_S
        if until <= since:
            raise CadError("to: after from")
        if until - since > span:
            since = until - span
        try:
            page = max(0, int(one("page", 6) or 0))
            size = min(PAGE_MAX, max(1, int(one("size", 4) or PAGE_SIZE)))
        except ValueError:
            raise CadError("page, size: numbers") from None
        levels, statuses = many("level", LEVELS), many("status", STATUSES)
        decided = many("decided", DECIDED)
        tags = many("tag", TAGS)
        held = [x for x in one("held", 200).split(",") if x]
        if any(not HELD_KEY.fullmatch(x) for x in held):       # (a value this page doesn't name yet still filters)
            raise CadError(f"held: {' | '.join(HELD)}")
        args = {"since": since, "until": until, **self.classes()}
        where = []
        if levels:
            where.append("(" + " OR ".join(LEVEL_SQL[x] for x in levels) + ")")
        if statuses:
            where.append(f"status IN ({', '.join(':st%d' % i for i in range(len(statuses)))})")
            args.update({f"st{i}": s for i, s in enumerate(statuses)})
        for k, col in (("category", "category"), ("pack", "pack"), ("source", "source_key"), ("system", "system")):
            v = one(k, 120)
            if v:
                where.append(f"{col} = :{k}")
                args[k] = v
        if decided:
            where.append(f"decided IN ({', '.join(':dc%d' % i for i in range(len(decided)))})")
            args.update({f"dc{i}": s for i, s in enumerate(decided)})
        if held:
            where.append(f"held IN ({', '.join(':hd%d' % i for i in range(len(held)))})")
            args.update({f"hd{i}": s for i, s in enumerate(held)})
        text = one("q", 200)
        if text:                       # the notes' words (sizeup/intel/notes.py), each word anywhere in a note
            for i, w in enumerate(text.split()[:8]):
                where.append(f"id IN (SELECT event_id FROM intel_notes WHERE text LIKE :nq{i} ESCAPE '\\')")
                args[f"nq{i}"] = "%" + re.sub(r"([\\%_])", r"\\\1", w) + "%"
        if tags:
            where.append("id IN (SELECT n.event_id FROM intel_notes n, json_each(n.tags) t WHERE t.value IN "
                         f"({', '.join(':tg%d' % i for i in range(len(tags)))}))")
            args.update({f"tg{i}": s for i, s in enumerate(tags)})
        if review:                     # tagged for a rule or model change and not acted on yet: by the NOTE's time (its
            # last change in the range), never the event's, so an item never drops out while it is kept (F3)
            where.append("id IN (SELECT n.event_id FROM intel_notes n, json_each(n.tags) t WHERE n.done = '' AND "
                         "n.updated_at >= :since AND n.updated_at < :until AND t.value IN "
                         f"({', '.join(':rv%d' % i for i in range(len(REVIEW_TAGS)))}))")
            args.update({f"rv{i}": s for i, s in enumerate(REVIEW_TAGS)})
        inner = (f"SELECT {', '.join('e.' + c.strip() for c in COLS.split(','))}, {HELD_SQL} AS held, "
                 f"{DECIDED_SQL} AS decided FROM intel_events e WHERE "
                 + ("1" if review else "e.first_at >= :since AND e.first_at < :until"))
        return {"inner": inner, "outer": " AND ".join(where) or "1", "args": args, "since": since, "until": until,
                "page": page, "size": size, "now": now}

    def list(self, q: dict, operator: str = "") -> dict:
        f = self.query(q)
        inner, outer, args, page, size = f["inner"], f["outer"], f["args"], f["page"], f["size"]
        store = self.cad.store
        rev = self._rev()
        # a poll (the page's own, ?poll=1) counts again only when the events changed: the same filters, the same total
        fkey = json.dumps([sorted((k, v) for k, v in q.items() if k not in ("page", "poll")), self.classes()])
        hit = self._totals.get(fkey)
        if str((q.get("poll") or [""])[0]) == "1" and hit is not None and hit[0] == rev:
            total = hit[1]
        else:
            total = store.one(f"SELECT COUNT(*) AS n FROM ({inner}) WHERE {outer}", args)["n"]
            if len(self._totals) > 64:
                self._totals.clear()
            self._totals[fkey] = (rev, total)
        rows = store.q(f"SELECT * FROM ({inner}) WHERE {outer} ORDER BY first_at DESC, id DESC LIMIT :lim OFFSET :off",
                       {**args, "lim": size, "off": page * size})
        names = source_names(self.cad)
        ids = [r["id"] for r in rows]
        votes, mine = self._votes(ids, operator)
        notes = self._notes(ids)
        out = [{**self.row(r, names, votes.get(r["id"], {}), mine.get(r["id"], [])),
                "notes": notes.get(r["id"], {"n": 0, "tags": [], "review": 0})} for r in rows]
        from sizeup.intel.notes import REVIEW_TAGS, TAGS
        return {"events": out, "total": total, "page": page, "size": size, "since": f["since"], "until": f["until"],
                "now": f["now"], "facets": self.facets(f["since"], f["until"], names), "held": held_options(),
                "levels": list(LEVELS), "decided": list(DECIDED), "statuses": list(STATUSES), "tags": list(TAGS),
                "review_tags": list(REVIEW_TAGS)}

    def _notes(self, ids: list[int]) -> dict:
        n = getattr(self.cad.intel_admin, "notes", None) if hasattr(self.cad, "intel_admin") else None
        return n.counts(ids) if n is not None else {}

    def ids(self, q: dict, limit: int) -> list[int]:
        """Every event id of the filters (all pages, newest first), at most limit: the export (notes.py)."""
        f = self.query(q)
        return [r["id"] for r in self.cad.store.q(
            f"SELECT id FROM ({f['inner']}) WHERE {f['outer']} ORDER BY first_at DESC, id DESC LIMIT :lim",
            {**f["args"], "lim": int(limit)})]

    def rows_by_id(self, ids: list[int]) -> list[dict]:
        """These events as the page's rows (level, would, held, decided ...), whatever their date."""
        if not ids:
            return []
        args = {"since": 0, "until": 1e12, **self.classes(), "ids": json.dumps([int(i) for i in ids])}
        inner = (f"SELECT {', '.join('e.' + c.strip() for c in COLS.split(','))}, {HELD_SQL} AS held, "
                 f"{DECIDED_SQL} AS decided FROM intel_events e WHERE e.id IN (SELECT value FROM json_each(:ids))")
        names = source_names(self.cad)
        return [self.row(r, names, {}, []) for r in self.cad.store.q(f"SELECT * FROM ({inner})", args)]

    def _votes(self, ids: list[int], operator: str) -> tuple[dict, dict]:
        """{event id: {verdict: n}} and {event id: [this reader's verdicts]} for the page's events (intel_feedback)."""
        if not ids:
            return {}, {}
        votes: dict = {}
        mine: dict = {}
        for f in self.cad.store.q(f"SELECT event_id, verdict, operator FROM intel_feedback WHERE event_id IN "
                                  f"({','.join('?' * len(ids))}) AND {VOTE_ROWS}", ids):
            v = votes.setdefault(f["event_id"], {})
            v[f["verdict"]] = v.get(f["verdict"], 0) + 1
            if operator and f["operator"] == operator and f["verdict"] not in mine.setdefault(f["event_id"], []):
                mine[f["event_id"]].append(f["verdict"])
        return votes, mine

    def row(self, r: dict, names: dict, votes: dict, mine: list) -> dict:
        try:
            rules = json.loads(r["rules"] or "[]")
        except ValueError:
            rules = []
        try:
            n = len(json.loads(r["traffic_ids"] or "[]"))
        except ValueError:
            n = 0
        p = self.engine.pack(r["pack"]) if hasattr(self.engine, "pack") else None
        cat = p.category(r["category"]) if p else {}
        from sizeup.intel.push import category_label
        key = r["held"] or ""
        held = None
        if r["pushed_at"] is None:
            label, tip = held_label(key, r["mode"])
            held = {"key": key, "label": label, "tip": tip, "detail": self._detail(key, r)}
        level = "push" if r["pushed_at"] is not None else "held" if r["mode"] == "push" else r["mode"]
        decided = r["decided"] or decided_by(rules)
        return {"id": r["id"], "at": r["first_at"], "last_at": r["last_at"], "status": r["status"], "pack": r["pack"],
                "pack_label": p.label if p else r["pack"], "category": r["category"],
                "label": category_label(cat, p.label if p else "", r["category"]), "level": level,
                "would": self.would({**r, "rules": rules}, key, decided) if held else "",
                "what": r["what_score"], "score_level": r["level"],
                "parts": [[str(x[0]), x[2] if len(x) > 2 else None] for x in rules if x],
                "decided": decided, "source_key": r["source_key"], "source": source_name(names, r["source_key"]),
                "system": r["system"], "place": r["label_full"] if r["precision"] != "none" else "",
                "precision": r["precision"], "confirmed": bool(r["confirmed"]), "town": r["town"], "held": held,
                "pushed_at": r["pushed_at"], "n": n, "votes": votes, "mine": mine,
                "near_incident": r["near_incident"], "incident_id": r["incident_id"]}

    @staticmethod
    def _detail(key: str, r: dict) -> str:
        if key == "uncorroborated":
            return "no cue · no rule · " + ("unplaced" if r["precision"] == "none" else "unconfirmed place")
        if key in HELD or not key:
            return ""
        return key                              # an engine's value this page doesn't know yet: as it is

    def facets(self, since: float, until: float, names: dict) -> dict:
        """The filters' choices in the range: categories (with their labels) and sources (named), by count. Read again
        only when the events changed or the range moved FACET_S (the last 24 h moves with every poll)."""
        key = (self._rev(), int(since // FACET_S), int(until // FACET_S))
        if self._facets is not None and self._facets[0] == key:
            return self._facets[1]
        out = self._facets_read(since, until, names)
        self._facets = (key, out)
        return out

    def _facets_read(self, since: float, until: float, names: dict) -> dict:
        cats: dict = {}
        srcs: dict = {}
        systems: dict = {}
        for g in self.cad.store.q("SELECT pack, category, source_key, system, COUNT(*) AS n FROM intel_events "
                                  "WHERE first_at >= ? AND first_at < ? GROUP BY pack, category, source_key, system",
                                  (since, until)):
            cats[g["category"]] = cats.get(g["category"], 0) + g["n"]
            if g["source_key"]:
                s = srcs.setdefault(g["source_key"], {"key": g["source_key"], "name": source_name(names, g["source_key"]),
                                                      "system": g["system"], "n": 0})
                s["n"] += g["n"]
            if g["system"]:
                systems[g["system"]] = systems.get(g["system"], 0) + g["n"]
        labels = {}
        for p in getattr(self.engine, "packs", []) or []:
            for c, d in p.categories.items():
                labels.setdefault(c, d.get("label") or c)
        return {"categories": [{"key": c, "label": labels.get(c, c), "n": n} for c, n in sorted(cats.items(), key=lambda x: -x[1])],
                "sources": sorted(srcs.values(), key=lambda s: -s["n"]),
                "systems": [{"key": s, "n": n} for s, n in sorted(systems.items(), key=lambda x: -x[1])]}

