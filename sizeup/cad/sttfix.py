"""Hand-corrected transcripts (radio intel plan, PR 7; the corrections side of #428): an admin opens a transmission's
transcript, writes what was really said, tags the spans that matter (an address, a callsign, a category word) and
marks it a reference. The rows are the private reference set for speech-to-text evaluation (tools/stt_eval.py) and
the "correction" of #428's training/eval bundles. They never go into the repo: they leave the hub only through the
admin-only JSONL export below.

    stt_corrections   one row per transmission (traffic_id = radio_traffic.id = the clip id, #428's clip_id):
                      original_hash  sha256 of the row's text when it was corrected (a re-run since: "stale")
                      corrected      the words as said
                      domain         fire | police | ems | atc | other (the row's service by default)
                      tags           JSON [{kind: address | callsign | category, start, end, value?}]: character spans of
                                     `corrected` (value: a category's name, an address as written)
                      by, at         who corrected it last, and when
                      reference      1: checked, a reference for the evaluation (#428's correction.checked)
                      based_on       the engine (radio_traffic.model / alt_model) whose text the editor started from

In cad.sqlite, beside radio_traffic, created IF NOT EXISTS on open (no schema version: an older release leaves the
table alone), keyed by the clip id: the transcripts it corrects live there, and so does everything that decides what
is kept (below). Written only through the CAD API's POSTs (admins), which the web server refuses while the hub's
single-writer fence is not held (sizeup/fence.py, sizeup/web/server.py Gate): leader-only and fenced like every
other admin edit.

Retention: a correction makes its clip a KEEPER. The corrected audio with its text is the training asset, so while
Settings > System > Transcript corrections > "Keep corrected clips" is on (the default; section "stt_corrections",
key "keep") a corrected transmission is never pruned from the radio log (Comms.prune) and its audio is a record,
kept the way a clip on a CAD event is (Comms.kept_for_events: the hub's audio retention, [storage]
audio_keep_event_days). Off: the transmission and its audio go by their channel's retention again, and the
correction goes with its transmission (ON DELETE CASCADE). An admin's explicit Delete removes the correction at
once, and the clip is an ordinary one again.

Every save and delete is in the Settings audit log (section "stt_corrections"), under the admin as every other
section records them (the signed-in operator, "Name <email>"), with the word and tag counts only: never the words,
nor any hash of them (the audit log is read more widely than transcripts are). A correction of another system's
transmission (police) is marked there, and a reader without a verified sign-in sees of it only that a correction
changed (redact_audit). `by` (the correction itself, the export) is the person's name only, never a sign-in email.

Another system's transmission (police: sourcekeys.is_context) is read and corrected only behind a verified sign-in
(sourcekeys.may_see_context), like the Radio page.

JSONL export (#428's agreed contract, one line per corrected clip):

    {clip_id, channel, talkgroup, talker, start, end, asr: {engine: text}, fixed_text,
     correction: {text, checked, by, at} | null}

    Extensions of that contract (a reader of #428's shape ignores them): `system` and `service` on each line (which
    radio system and service the clip is from: westchester / fire, another system / police ...), and
    `correction.based_on` (the engine whose text the editor started from). At most EXPORT_MAX lines: when there are
    more, the response says so (X-Sizeup-Truncated: <the limit>, X-Sizeup-Next-Since: <unix s>) and the file ends
    with one line {"truncated": true, "limit", "next_since"}: ask again with ?since=<next_since> for the rest.

asr: each engine's text as stored (radio_traffic.text under its model, alt_text under alt_model: the "both" mode);
by: the corrector's name (never their sign-in email); fixed_text: the text as the Radio page shows it (status words put back, sizeup/cad/statushints.py), else the text.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from sizeup import sourcekeys
from sizeup.cad.comms import CORRECTIONS_KEEP, CORRECTIONS_SECTION
from sizeup.cad.premise import who
from sizeup.cad.settings import REMOVE, Editor
from sizeup.cad.store import TZ, CadError

SECTION = "stt_corrections"           # the Settings audit log's section; and the overrides' (key "keep")
assert SECTION == CORRECTIONS_SECTION
REDACTED = "another system's transmission"     # an audit row's key for a reader who may not see that system
DOMAINS = ("fire", "police", "ems", "atc", "other")
TAG_KINDS = ("address", "callsign", "category")
TEXT_MAX = 2000
TAGS_MAX = 40
VALUE_MAX = 120
EXPORT_MAX = 50000
TID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stt_corrections (
  traffic_id TEXT PRIMARY KEY REFERENCES radio_traffic(id) ON DELETE CASCADE,
  original_hash TEXT NOT NULL DEFAULT '',  -- sha256 (hex, 16) of radio_traffic.text when corrected
  corrected TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT 'other',    -- fire | police | ems | atc | other
  tags TEXT NOT NULL DEFAULT '[]',         -- [{kind, start, end, value?}] spans of corrected
  "by" TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL,
  reference INTEGER NOT NULL DEFAULT 0,    -- 1: checked, in the evaluation's reference set
  based_on TEXT NOT NULL DEFAULT ''        -- the engine whose text the editor started from (a model name), '' unknown
);
CREATE INDEX IF NOT EXISTS stt_corrections_at ON stt_corrections(at);
"""


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def domain_of(row: dict) -> str:
    """The row's service as a domain: air -> atc; fire, police, ems as they are; anything else (transit, '', unknown):
    other (the admin picks)."""
    s = row.get("service") or ""
    return "atc" if s == "air" else s if s in ("fire", "police", "ems") else "other"


EMAIL = re.compile(r"[^\s@<>]+@[^\s@<>]+")


def name_only(operator: str) -> str:
    """A person's name for the record: 'Name <email>' -> 'Name'; a bare email (an admin not named yet) -> ''."""
    n = who(operator or "")
    return "" if EMAIL.search(n) else n


def clean_tags(tags, corrected: str) -> list[dict]:
    """[{kind, start, end, value?}] checked against the corrected text: CadError on anything else."""
    if tags is None:
        return []
    if not isinstance(tags, list) or len(tags) > TAGS_MAX:
        raise CadError(f"tags: a list of at most {TAGS_MAX}")
    out = []
    for t in tags:
        if not isinstance(t, dict) or t.get("kind") not in TAG_KINDS:
            raise CadError(f"tags: each {{kind: {' | '.join(TAG_KINDS)}, start, end}}")
        s, e = t.get("start"), t.get("end")
        if not (isinstance(s, int) and isinstance(e, int)) or isinstance(s, bool) or isinstance(e, bool) \
                or not 0 <= s < e <= len(corrected):
            raise CadError("tags: start < end, within the corrected text")
        d = {"kind": t["kind"], "start": s, "end": e}
        v = t.get("value")
        if v not in (None, ""):
            if not isinstance(v, str) or len(v) > VALUE_MAX:
                raise CadError(f"tags: value, text of at most {VALUE_MAX}")
            d["value"] = v.strip()
        out.append(d)
    out.sort(key=lambda d: (d["start"], d["end"], d["kind"]))
    return out


def _when(v, what: str) -> float | None:
    """unix seconds, or an ISO date / time (the department's zone); '' or None: no bound."""
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        raise CadError(f"{what}: unix seconds or an ISO date") from None
    return (d if d.tzinfo else d.replace(tzinfo=TZ)).timestamp()


class SttCorrections:
    """One per Cad (sizeup/cad/service.py)."""

    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        with self.store.lock:
            if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'radio_traffic'").fetchone():
                self.store.db.executescript(SCHEMA)
                # a table made before based_on (a build of this branch): the column added once, nothing rewritten
                if "based_on" not in {r[1] for r in self.store.db.execute("PRAGMA table_info(stt_corrections)")}:
                    self.store.db.execute("ALTER TABLE stt_corrections ADD COLUMN based_on TEXT NOT NULL DEFAULT ''")

    def keep(self) -> bool:
        """Settings > System > Transcript corrections > Keep corrected clips (default on): see the module doc."""
        return self.cad.comms.corrections_kept()

    def settings_op(self, action: str, body: dict, op: str) -> dict:
        """POST /api/cad/settings/stt_corrections {action: set, key: keep, value: true | false} | {action: reset, key:
        keep}: admins. Audited ("system")."""
        if body.get("key") != CORRECTIONS_KEEP:
            raise CadError(f"key: {CORRECTIONS_KEEP}")
        cur = self.keep()
        if action == "reset":
            v = True
        elif action == "set":
            v = body.get("value")
            if not isinstance(v, bool):
                raise CadError(f"{CORRECTIONS_KEEP}: true or false")
        else:
            raise CadError("action: set or reset")
        out = self.cad.settings._commit([(SECTION, CORRECTIONS_KEEP, REMOVE if v is True else v)], op,
                                        [("system", f"{SECTION}/{CORRECTIONS_KEEP}", "on" if cur else "off", "on" if v else "off")])
        return {**out, "stt_corrections": self.counts()}

    # -- reading ------------------------------------------------------------------------------------------------------

    def _traffic(self, tid: str) -> dict:
        if not isinstance(tid, str) or not TID.fullmatch(tid):
            raise CadError("no such transmission")
        r = self.store.one("SELECT id, start, end, channel, talkgroup, talker, radio_id, text, model, alt_text, alt_model, "
                           "source, system, service, status FROM radio_traffic WHERE id = ?", (tid,))
        if r is None or (sourcekeys.is_context(r) and not sourcekeys.may_see_context()):
            raise CadError("no such transmission")          # another system's: only behind a sign-in (#419)
        self._talkers([r])
        return r

    def _talkers(self, rows: list[dict]) -> None:
        """Each row's talker label as Settings > Radios > Radio IDs names it now (Comms.talkers; in place)."""
        fn = getattr(getattr(self.cad, "comms", None), "talkers", None)
        if fn is not None:
            try:
                fn(rows)
            except Exception:  # noqa: BLE001 - the labels as stored
                pass

    @staticmethod
    def _public(c: dict | None) -> dict | None:
        if c is None:
            return None
        return {"corrected": c["corrected"], "domain": c["domain"], "tags": json.loads(c["tags"] or "[]"),
                "by": name_only(c["by"]), "at": c["at"], "reference": bool(c["reference"]),
                "original_hash": c["original_hash"], "based_on": c["based_on"]}

    def get(self, tid: str) -> dict:
        """GET /api/cad/stt/correction/<id>: the transmission's transcripts and its correction (null: none yet)."""
        r = self._traffic(tid)
        c = self.store.one("SELECT * FROM stt_corrections WHERE traffic_id = ?", (tid,))
        out = self._public(c)
        if out is not None:
            out["stale"] = out["original_hash"] != text_hash(r["text"])     # re-transcribed since
        # what the editor starts from: the text of the engine that produced the row (its model), else the other's
        first = ({"text": r["text"], "engine": r["model"]} if r["text"] or not r["alt_text"]
                 else {"text": r["alt_text"], "engine": r["alt_model"]})
        return {"traffic_id": tid, "start": r["start"], "channel": r["channel"], "talker": r["talker"],
                "text": r["text"], "model": r["model"], "alt_text": r["alt_text"], "alt_model": r["alt_model"],
                "prefill": first,
                "domain": domain_of(r), "correction": out, "domains": list(DOMAINS), "tag_kinds": list(TAG_KINDS)}

    def counts(self) -> dict:
        """GET /api/cad/stt/corrections: how many, how many references, per domain (Settings > System)."""
        visible = "" if sourcekeys.may_see_context() else f"AND t.system IN {sourcekeys.OURS_SQL}"
        rows = self.store.q(f"SELECT c.domain, COUNT(*) AS n, SUM(c.reference) AS ref, MAX(c.at) AS last FROM stt_corrections c "
                            f"JOIN radio_traffic t ON t.id = c.traffic_id WHERE 1 {visible} GROUP BY c.domain")
        n, keep = sum(r["n"] for r in rows), self.keep()
        return {"n": n, "reference": sum(r["ref"] or 0 for r in rows), "keep": keep, "kept": n if keep else 0,
                "last": max((r["last"] for r in rows), default=None),
                "domains": {r["domain"]: {"n": r["n"], "reference": r["ref"] or 0} for r in rows}}

    # -- writing (admins; fenced by the server) -------------------------------------------------------------------------

    def save(self, tid: str, body: dict) -> dict:
        """POST /api/cad/stt/correction/<id> {corrected, domain?, tags?, reference?, based_on?} | {delete: true}."""
        admin = str(body.get("operator") or "")[:200]           # the audit's: who, as every section records them
        op = name_only(admin)                                    # the correction's `by`: the name only, never an email
        r = self._traffic(tid)
        context = sourcekeys.is_context(r)
        if body.get("delete") is True:
            return self.delete(tid, admin, context)
        corrected = body.get("corrected")
        if not isinstance(corrected, str) or not corrected.strip() or len(corrected) > TEXT_MAX:
            raise CadError(f"corrected: the words as said, 1 to {TEXT_MAX} characters")
        corrected = corrected.strip()
        domain = body.get("domain") or domain_of(r)
        if domain not in DOMAINS:
            raise CadError(f"domain: one of {', '.join(DOMAINS)}")
        tags = clean_tags(body.get("tags"), corrected)
        ref = body.get("reference", False)
        if not isinstance(ref, bool):
            raise CadError("reference: true or false")
        based = body.get("based_on")
        if based in (None, ""):
            based = ""
        elif based not in {m for m in (r["model"], r["alt_model"]) if m}:
            raise CadError("based_on: one of this transmission's engines")
        now = self.store.clock()
        h = text_hash(r["text"])
        with self.store.tx() as db:
            old = db.execute("SELECT * FROM stt_corrections WHERE traffic_id = ?", (tid,)).fetchone()
            if not based and old is not None:
                based = old["based_on"]              # an edit of a correction: still from the text it started from
            db.execute('INSERT INTO stt_corrections (traffic_id, original_hash, corrected, domain, tags, "by", at, reference, '
                       "based_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(traffic_id) DO UPDATE SET "
                       "original_hash = excluded.original_hash, corrected = excluded.corrected, domain = excluded.domain, "
                       'tags = excluded.tags, "by" = excluded."by", at = excluded.at, reference = excluded.reference, '
                       "based_on = excluded.based_on",
                       (tid, h, corrected, domain, json.dumps(tags, separators=(",", ":")), op, now, int(ref), based))
            Editor._audit(db, now, admin, SECTION, tid, self._audit_view(dict(old), context) if old else None,
                          self._audit_view({"corrected": corrected, "domain": domain, "tags": tags, "reference": ref,
                                            "based_on": based}, context))
        return {"ok": True, **self.get(tid)}

    def delete(self, tid: str, admin: str, context: bool = False) -> dict:
        """An admin's explicit delete: the correction goes now (and its clip is an ordinary one again)."""
        now = self.store.clock()
        with self.store.tx() as db:
            old = db.execute("SELECT * FROM stt_corrections WHERE traffic_id = ?", (tid,)).fetchone()
            if old is None:
                raise CadError("no correction to delete")
            db.execute("DELETE FROM stt_corrections WHERE traffic_id = ?", (tid,))
            Editor._audit(db, now, admin, SECTION, tid, self._audit_view(dict(old), context), None)
        return {"ok": True, **self.get(tid)}

    @staticmethod
    def _audit_view(c: dict, context: bool = False) -> dict:
        """What the audit log keeps of a correction: counts only. Never its words, nor any hash of them or of the
        transcript. context: another system's transmission (redact_audit)."""
        tags = c["tags"] if isinstance(c["tags"], list) else json.loads(c["tags"] or "[]")
        kinds: dict[str, int] = {}
        for t in tags:
            kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
        out = {"words": words(c["corrected"]), "domain": c["domain"], "tags": kinds, "reference": bool(c["reference"]),
               "based_on": c.get("based_on") or ""}
        if context:
            out["context"] = True
        return out

    def redact_audit(self, rows: list[dict]) -> list[dict]:
        """The Settings audit log as this reader may see it (Cad.settings_audit_log): a correction of another system's
        transmission, for a reader without a verified sign-in (sourcekeys.may_see_context), is only "a correction
        changed": who and when, never the clip's id, its domain or its counts. A row is another system's when it says
        so (context) or its transmission is one now; and, to be safe, when neither can be told any more."""
        mine = [r for r in rows if r.get("section") == SECTION]
        if not mine or sourcekeys.may_see_context():
            return rows

        def marked(v) -> bool | None:
            try:
                d = json.loads(v) if v else None
            except ValueError:
                return None
            return bool(d.get("context")) if isinstance(d, dict) else None
        ids = sorted({str(r.get("key") or "") for r in mine})
        ours: set[str] = set()
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            ours.update(r["id"] for r in self.store.q(
                f"SELECT id FROM radio_traffic WHERE id IN ({','.join('?' * len(chunk))}) AND system IN {sourcekeys.OURS_SQL}",
                chunk))
        out = []
        for r in rows:
            if r.get("section") == SECTION:
                flags = [f for f in (marked(r.get("old")), marked(r.get("new"))) if f is not None]
                if any(flags) or str(r.get("key") or "") not in ours:
                    r = dict(r, key=REDACTED, old="" if not r.get("old") else '{"changed":true}',
                             new="" if not r.get("new") else '{"changed":true}')
            out.append(r)
        return out

    # -- the JSONL export (#428's contract; admins, ADMIN_READS) -------------------------------------------------------

    def export_rows(self, q: dict) -> list[dict]:
        return self.export_page(q)[0]

    def export_page(self, q: dict, limit: int | None = None) -> tuple[list[dict], float | None]:
        """The corrected clips matching ?since=&until=&channel=&reference=1, oldest first, each as #428's JSONL record;
        and, when there are more than `limit` (EXPORT_MAX), where the rest starts (?since= for the next request: the
        first clip left out's start; None: that was all)."""
        limit = EXPORT_MAX if limit is None else limit
        def one(k: str) -> str:
            return str((q.get(k) or [""])[0]).strip()
        where, args = ["1"], []
        if (s := _when(one("since"), "since")) is not None:
            where.append("t.start >= ?")
            args.append(s)
        if (u := _when(one("until"), "until")) is not None:
            where.append("t.start < ?")
            args.append(u)
        if ch := one("channel")[:80]:
            where.append("t.channel = ?")
            args.append(ch)
        if one("reference") in ("1", "true"):
            where.append("c.reference = 1")
        if not sourcekeys.may_see_context():
            where.append(f"t.system IN {sourcekeys.OURS_SQL}")
        rows = self.store.q("SELECT t.id, t.channel, t.talkgroup, t.talker, t.radio_id, t.start, t.end, t.text, t.model, t.alt_text, "
                            "t.alt_model, t.dispatcher, t.rig, t.source, t.system, t.service, "
                            "t.corr_text, t.corr_rules, c.corrected, c.reference, "
                            "c.\"by\", c.at, c.based_on "
                            "FROM stt_corrections c JOIN radio_traffic t ON t.id = c.traffic_id "
                            f"WHERE {' AND '.join(where)} ORDER BY t.start, t.id LIMIT ?", (*args, limit + 1))
        more = len(rows) > limit
        more_start = rows[limit]["start"] if more else None
        rows = rows[:limit]
        # the rest starts at the first clip left out (a clip sharing the last one's start is then sent again, never lost)
        next_since = more_start if more else None
        for r in rows:                         # (the label it was recorded with: a snapshot stays reproducible, #977)
            r["talker_recorded"] = r.get("talker") or ""
        self._talkers(rows)                    # the talker label as the Radio IDs table gives it now
        fix = getattr(getattr(self.cad, "hints", None), "decorate", None)
        if fix is not None:
            try:
                fix(rows)                      # in place: "fixed_text" on the rows whose status words were put back
            except Exception:  # noqa: BLE001  (the shown text is a nicety: the export never fails for it)
                pass
        return [record(r) for r in rows], next_since

    def export_jsonl(self, q: dict, limit: int | None = None) -> tuple[bytes, int, dict]:
        """(the JSONL, how many clips, the extra headers). Truncated at the limit: said in the headers and in a last
        line {"truncated": true, "limit", "next_since"} (never silently)."""
        limit = EXPORT_MAX if limit is None else limit
        rows, next_since = self.export_page(q, limit)
        lines = [json.dumps(r, separators=(",", ":"), ensure_ascii=False) for r in rows]
        headers = {}
        if next_since is not None:
            lines.append(json.dumps({"truncated": True, "limit": limit, "next_since": next_since}, separators=(",", ":")))
            headers = {"X-Sizeup-Truncated": str(limit), "X-Sizeup-Next-Since": repr(float(next_since))}
        return "".join(ln + "\n" for ln in lines).encode(), len(rows), headers


def record(r: dict) -> dict:
    """One clip as #428's JSONL line. r: radio_traffic's columns (+ fixed_text when the status words were put back) and
    the correction's (corrected, reference, by, at; corrected None: no correction)."""
    asr = {}
    if r.get("text") or r.get("model"):          # an engine that ran and heard nothing: "" (all deletions)
        asr[r.get("model") or "unknown"] = r.get("text") or ""
    if r.get("alt_text") or r.get("alt_model"):
        asr[r.get("alt_model") or "alt"] = r.get("alt_text") or ""
    corr = None
    if r.get("corrected") is not None:
        corr = {"text": r["corrected"], "checked": bool(r.get("reference")), "by": name_only(r.get("by") or ""), "at": r.get("at"),
                "based_on": r.get("based_on") or ""}
    return {"clip_id": r["id"], "channel": r.get("channel") or "", "talkgroup": r.get("talkgroup"),
            "talker": r.get("talker") or "", "start": r.get("start"), "end": r.get("end"), "asr": asr,
            # (#977) its radio ID(s), and the talker label it was recorded with ("talker": as the Radio IDs table
            # names it at the export)
            "radio_id": r.get("radio_id") or "", "talker_recorded": r.get("talker_recorded", r.get("talker")) or "",
            "fixed_text": r.get("fixed_text") or r.get("text") or "", "correction": corr,
            "system": r.get("system") or "", "service": r.get("service") or ""}      # (extensions of #428's contract)
