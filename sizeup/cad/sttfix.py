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

In cad.sqlite, beside radio_traffic, created IF NOT EXISTS on open (no schema version: an older release leaves the
table alone). Its retention is the transcript's: the row goes with its transmission (ON DELETE CASCADE, the radio
log's prune, sizeup/cad/comms.py). Written only through the CAD API's POSTs (admins), which the web server refuses
while the hub's single-writer fence is not held (sizeup/fence.py, sizeup/web/server.py Gate): leader-only and fenced
like every other admin edit. Every save and delete is in the Settings audit log (section "stt_corrections"), with
the word and tag counts and the original text's hash only: never the words, nor a hash of them (the audit log is read
more widely than transcripts are). `by` is the person's name only, never a sign-in email.

Another system's transmission (police: sourcekeys.is_context) is read and corrected only behind a verified sign-in
(sourcekeys.may_see_context), like the Radio page.

JSONL export (#428's agreed contract, one line per corrected clip):

    {clip_id, channel, talkgroup, talker, start, end, asr: {engine: text}, fixed_text,
     correction: {text, checked, by, at} | null}

asr: each engine's text as stored (radio_traffic.text under its model, alt_text under alt_model: the "both" mode);
by: the corrector's name (never their sign-in email); fixed_text: the text as the Radio page shows it (status words put back, sizeup/cad/statushints.py), else the text.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from sizeup import sourcekeys
from sizeup.cad.premise import who
from sizeup.cad.settings import Editor
from sizeup.cad.store import TZ, CadError

SECTION = "stt_corrections"           # the Settings audit log's section
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
  reference INTEGER NOT NULL DEFAULT 0     -- 1: checked, in the evaluation's reference set
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

    # -- reading ------------------------------------------------------------------------------------------------------

    def _traffic(self, tid: str) -> dict:
        if not isinstance(tid, str) or not TID.fullmatch(tid):
            raise CadError("no such transmission")
        r = self.store.one("SELECT id, start, end, channel, talkgroup, talker, text, model, alt_text, alt_model, source, "
                           "system, service, status FROM radio_traffic WHERE id = ?", (tid,))
        if r is None or (sourcekeys.is_context(r) and not sourcekeys.may_see_context()):
            raise CadError("no such transmission")          # another system's: only behind a sign-in (#419)
        return r

    @staticmethod
    def _public(c: dict | None) -> dict | None:
        if c is None:
            return None
        return {"corrected": c["corrected"], "domain": c["domain"], "tags": json.loads(c["tags"] or "[]"),
                "by": name_only(c["by"]), "at": c["at"], "reference": bool(c["reference"]),
                "original_hash": c["original_hash"]}

    def get(self, tid: str) -> dict:
        """GET /api/cad/stt/correction/<id>: the transmission's transcripts and its correction (null: none yet)."""
        r = self._traffic(tid)
        c = self.store.one("SELECT * FROM stt_corrections WHERE traffic_id = ?", (tid,))
        out = self._public(c)
        if out is not None:
            out["stale"] = out["original_hash"] != text_hash(r["text"])     # re-transcribed since
        return {"traffic_id": tid, "start": r["start"], "channel": r["channel"], "talker": r["talker"],
                "text": r["text"], "model": r["model"], "alt_text": r["alt_text"], "alt_model": r["alt_model"],
                "domain": domain_of(r), "correction": out, "domains": list(DOMAINS), "tag_kinds": list(TAG_KINDS)}

    def counts(self) -> dict:
        """GET /api/cad/stt/corrections: how many, how many references, per domain (Settings > System)."""
        visible = "" if sourcekeys.may_see_context() else f"AND t.system IN {sourcekeys.OURS_SQL}"
        rows = self.store.q(f"SELECT c.domain, COUNT(*) AS n, SUM(c.reference) AS ref, MAX(c.at) AS last FROM stt_corrections c "
                            f"JOIN radio_traffic t ON t.id = c.traffic_id WHERE 1 {visible} GROUP BY c.domain")
        return {"n": sum(r["n"] for r in rows), "reference": sum(r["ref"] or 0 for r in rows),
                "last": max((r["last"] for r in rows), default=None),
                "domains": {r["domain"]: {"n": r["n"], "reference": r["ref"] or 0} for r in rows}}

    # -- writing (admins; fenced by the server) -------------------------------------------------------------------------

    def save(self, tid: str, body: dict) -> dict:
        """POST /api/cad/stt/correction/<id> {corrected, domain?, tags?, reference?} | {delete: true}."""
        op = name_only(str(body.get("operator") or "")[:200])    # the name only: never a sign-in email
        r = self._traffic(tid)
        if body.get("delete") is True:
            return self.delete(tid, op)
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
        now = self.store.clock()
        h = text_hash(r["text"])
        with self.store.tx() as db:
            old = db.execute("SELECT * FROM stt_corrections WHERE traffic_id = ?", (tid,)).fetchone()
            db.execute('INSERT INTO stt_corrections (traffic_id, original_hash, corrected, domain, tags, "by", at, reference) '
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(traffic_id) DO UPDATE SET original_hash = excluded.original_hash, "
                       'corrected = excluded.corrected, domain = excluded.domain, tags = excluded.tags, "by" = excluded."by", '
                       "at = excluded.at, reference = excluded.reference",
                       (tid, h, corrected, domain, json.dumps(tags, separators=(",", ":")), op, now, int(ref)))
            Editor._audit(db, now, op, SECTION, tid, self._audit_view(dict(old)) if old else None,
                            self._audit_view({"corrected": corrected, "domain": domain, "tags": tags, "reference": ref,
                                              "original_hash": h}))
        return {"ok": True, **self.get(tid)}

    def delete(self, tid: str, op: str) -> dict:
        now = self.store.clock()
        with self.store.tx() as db:
            old = db.execute("SELECT * FROM stt_corrections WHERE traffic_id = ?", (tid,)).fetchone()
            if old is None:
                raise CadError("no correction to delete")
            db.execute("DELETE FROM stt_corrections WHERE traffic_id = ?", (tid,))
            Editor._audit(db, now, op, SECTION, tid, self._audit_view(dict(old)), None)
        return {"ok": True, **self.get(tid)}

    @staticmethod
    def _audit_view(c: dict) -> dict:
        """What the audit log keeps of a correction: never its words."""
        tags = c["tags"] if isinstance(c["tags"], list) else json.loads(c["tags"] or "[]")
        kinds: dict[str, int] = {}
        for t in tags:
            kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
        return {"words": words(c["corrected"]), "domain": c["domain"], "tags": kinds, "reference": bool(c["reference"]),
                "original_hash": c["original_hash"]}

    # -- the JSONL export (#428's contract; admins, ADMIN_READS) -------------------------------------------------------

    def export_rows(self, q: dict) -> list[dict]:
        """The corrected clips matching ?since=&until=&channel=&reference=1, oldest first (at most EXPORT_MAX), each as
        #428's JSONL record."""
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
        rows = self.store.q("SELECT t.id, t.channel, t.talkgroup, t.talker, t.start, t.end, t.text, t.model, t.alt_text, "
                            "t.alt_model, t.dispatcher, t.rig, t.source, t.system, c.corrected, c.reference, c.\"by\", c.at "
                            "FROM stt_corrections c JOIN radio_traffic t ON t.id = c.traffic_id "
                            f"WHERE {' AND '.join(where)} ORDER BY t.start LIMIT ?", (*args, EXPORT_MAX))
        fix = getattr(getattr(self.cad, "hints", None), "decorate", None)
        if fix is not None:
            try:
                fix(rows)                      # in place: "fixed_text" on the rows whose status words were put back
            except Exception:  # noqa: BLE001  (the shown text is a nicety: the export never fails for it)
                pass
        return [record(r) for r in rows]

    def export_jsonl(self, q: dict) -> tuple[bytes, int]:
        rows = self.export_rows(q)
        return "".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows).encode(), len(rows)


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
        corr = {"text": r["corrected"], "checked": bool(r.get("reference")), "by": name_only(r.get("by") or ""), "at": r.get("at")}
    return {"clip_id": r["id"], "channel": r.get("channel") or "", "talkgroup": r.get("talkgroup"),
            "talker": r.get("talker") or "", "start": r.get("start"), "end": r.get("end"), "asr": asr,
            "fixed_text": r.get("fixed_text") or r.get("text") or "", "correction": corr}
