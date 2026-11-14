"""Who was dispatched (#370): a whole department, or specific units. One label per incident, shown on the incident and
its queue card (inc["scope"]).

    page   the page is usually heard first and sets it at once: apparatus named in it -> "Specific units", and only
           then. None named: "Full department (<dept>)" for each department whose tones were heard, when it gives a
           call type, says so ("full department", as speech-to-text hears it too: said_full), or the tones are a
           department's own all-call (all_call: never a role's) and it isn't a test or an announcement; otherwise
           nothing
    feed   an external CAD feed's report (its best-ranked one), read by from_units: a department-level dispatch -> "Full
           department (<dept>)": a department entry (Sources.department_units: a code that means a whole department), a
           department's placeholder on its unit list ("department dispatched, no unit yet": Lists.placeholder, its
           department), or a comment that says so ("general alarm", FEED_FULL); apparatus with none of those ->
           "Specific units". A department-level dispatch stays one when its units sign on and the placeholder leaves
    both   the county feed is the primary source and the page the backup (sizeup/cad/sources.py): a feed's
           department-level dispatch wins (shown()): over a page with no reading, and over a page that named apparatus
           when the feed names a department whose tones the page carried (a comment alone, or another department's
           entry on a mutual-aid call, never overrides apparatus a page named). Two "full" readings: both departments.
           A feed's apparatus list never changes a page's reading: the page is the dispatch itself, and units listed
           as they sign on are no evidence against a full department. A disagreement is one terse timeline line (kind
           "feed"): "Page: all PCFD · feed: units" (the page heard after the feed), "Feed: units · page: all PCFD"
           (the feed after it)
    later  a later page's reading replaces an earlier one, upgrade or downgrade ("Page: units → all PCFD"), on the
           incident it is matched to whatever its status; a feed's apparatus list never changes a page's. An upgrade (specific units
           -> full department) needs evidence (upgrade()): a repeat that only lost the unit names (speech-to-text)
           is not one. Becoming full department widens the radio log's backfill to the paged departments' units
           (sizeup/cad/radiolog.py); a downgrade takes back what only widening linked. One disagreement between page
           and feed is one line, whichever came first (_noted)

Shown short ("Full dept", "Specific units"; short()), "Paged: full department (<dept>)" in its hover (label()); the
timeline says it "all PCFD" / "units" (_said()). A page on a feed's incident (one it reached, or its card moved onto)
says who it paged in one timeline line of kind "tones" (page_line), once.
"""

from __future__ import annotations

import json
import re

from sizeup.cad.fuzzy import skeleton
from sizeup.cad.sources import PAGING, dept_code, is_placeholder
from sizeup.radio.tones import base_name

FULL, UNITS = "full", "units"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatch_scope (
  incident_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                   -- full | units
  departments TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL,                 -- page | feed: who set it (a page's reading stands)
  at REAL NOT NULL,
  page TEXT NOT NULL DEFAULT '',        -- the page's own reading [kind, departments, its line, the tones heard so far]:
                                        -- it stands (#370 review);
                                        -- a moved card's goes with it
  feed TEXT NOT NULL DEFAULT ''         -- the feed's reading [kind, departments]: shown only with no page reading
);
"""


def _parse(v) -> tuple[str, list] | None:
    try:
        x = json.loads(v) if v else None
    except ValueError:
        return None
    return (x[0], list(x[1])) if isinstance(x, list) and len(x) >= 2 else None


def label(kind: str, departments) -> str:
    if kind == FULL:
        return f"Full department ({', '.join(departments)})" if departments else "Full department"
    return "Specific units" if kind == UNITS else ""


def abbrev(name: str) -> str:
    """'Port Chester FD' -> 'PCFD'; a tone book entity 'Port Chester · Fire' -> 'PCFD'; 'Rye Brook' -> 'RB'."""
    place, _, service = str(name or "").partition(" · ")
    words = re.findall(r"[A-Za-z0-9]+", place)
    out = "".join(w if w.isupper() and len(w) > 1 else w[0].upper() for w in words)
    if service:
        out += "FD" if service.split(" · ")[0] == "Fire" else "".join(w[0].upper() for w in re.findall(r"[A-Za-z]+", service))
    return out or str(name or "")


def short(kind: str, departments=()) -> str:
    """The chip: 'Full dept' / 'Specific units'."""
    return {FULL: "Full dept", UNITS: "Specific units"}.get(kind, "")


def _said(kind: str, departments) -> str:
    """As the timeline says it: 'units', 'all PCFD', 'all PCFD, RBFD'."""
    if kind == FULL:
        return "all " + ", ".join(abbrev(d) for d in departments) if departments else "all"
    return "units" if kind == UNITS else ""


def all_call(tone_departments) -> list[str]:
    """The departments whose own all-call tones were heard: "Port Chester FD", the tone book's "Port Chester · Fire".
    Never a role's pair ("Port Chester · Fire · Chiefs") nor another service's ("... · EMS")."""
    out = []
    for d in tone_departments or ():
        n = str(d or "").removesuffix(" (heard)").removesuffix(" (est.)")
        parts = n.split(" · ")
        if n and (len(parts) == 1 or (len(parts) == 2 and parts[1] == "Fire")) and n not in out:
            out.append(n)
    return out


# A page with a department's tones that is no dispatch: its tones alone never make it a full department
NOT_A_CALL = re.compile(r"\bon a test\b|\btest only\b|\bthis is a test\b|\btest page\b|\bdrill\b|\bannouncement\b", re.I)


def from_page(tone_departments, units, incident_type: str, text: str = "") -> tuple[str, list[str]] | None:
    """(kind, departments) as the page says it, else None. "Specific units" only when it names apparatus; text: the
    page's words, for a full department said and for the all-call default (see the module doc)."""
    depts = list(dict.fromkeys(base_name(d) for d in tone_departments or () if d))
    if units:
        return UNITS, []
    if not depts:
        return None
    if (incident_type and incident_type != "Unknown") or said_full(text, depts):
        return FULL, depts
    own = list(dict.fromkeys(base_name(d) for d in all_call(tone_departments)))
    if own and not NOT_A_CALL.search(text or ""):
        return FULL, own
    return None


# A department-level dispatch said in a feed's comment
FEED_FULL = re.compile(r"\bgeneral\s+(?:fire\s+)?alarm\b|\bfull\s+(?:department|dept|response|assignment)\b|\ball[\s-]+hands\b",
                       re.I)


def from_units(units, departments: dict[str, str], placeholder_of=None, comments=()) -> tuple[str, list[str]] | None:
    """(kind, departments) from a feed's report, else None: its unit list against its department entries {code:
    department} and the departments' placeholders (placeholder_of(code) -> its department's name, "" for a code that
    is no placeholder or names none), and its comments (FEED_FULL: full, naming no department by itself)."""
    codes = {dept_code(k): v for k, v in departments.items() if dept_code(k)}
    depts = []
    for u in units or ():
        name = codes.get(dept_code(u)) or (placeholder_of(u) if placeholder_of is not None else "")
        if name and name not in depts:
            depts.append(name)
    if depts or any(FEED_FULL.search(str(c[1] if isinstance(c, (list, tuple)) and len(c) > 1 else c)) for c in comments or ()):
        return FULL, depts
    if any(dept_code(u) and not is_placeholder(u) for u in units or ()):    # a placeholder is never "Specific units"
        return UNITS, []
    return None


def _merged(a, b) -> list[str]:
    """Two readings' departments as one list, one entry per department (dept_key), a's first."""
    out, seen = [], set()
    for d in [*a, *b]:
        k = dept_key(d) or d
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _differ(page, feed) -> bool:
    """The page's and the feed's readings say otherwise (a feed's "full" from a comment names no department: it
    agrees with any full department the page read)."""
    if page is None or feed is None or tuple(page) == tuple(feed):
        return False
    return not (page[0] == feed[0] == FULL and not feed[1])


def shown(page, feed, tones=()) -> tuple[str, tuple[str, list] | None]:
    """("page" | "feed" | "", the reading shown) from the page's and the feed's ((kind, departments) or None); tones:
    the departments whose tones the page carried. The module doc's "both"."""
    if feed is not None and feed[0] == FULL:
        if page is None:
            return "feed", feed
        if page[0] == FULL:
            return "page", (FULL, _merged(page[1], feed[1]))
        heard = {dept_key(t) for t in tones or ()} - {""}
        if feed[1] and (not heard or heard & {dept_key(d) for d in feed[1]}):
            return "feed", feed
    if page is not None:
        return "page", page
    return ("feed", feed) if feed is not None else ("", None)


def page_line(departments, units, tone_departments, incident_type: str, feed: str = "", text: str = "") -> str:
    """One timeline line for who a page paged (#370, with #365's page on a feed's incident): "Page: all PCFD",
    "Page: PCFD · units Engine 60, Ladder 31", "Page: tones PCFD" (a test, a role's tones), and " · feed: units" when
    the feed's label says otherwise. "" when it named nobody."""
    got = from_page(tone_departments, units, incident_type, text)
    names = ", ".join(abbrev(base_name(d)) for d in dict.fromkeys(departments or ()) if d)
    if got is not None and got[0] == FULL:
        said = _said(*got)
    elif units:
        said = (f"{names} · " if names else "") + f"units {', '.join(units)}"
    elif names:
        said = f"tones {names}"
    else:
        return ""
    return f"Page: {said}" + (f" · feed: {feed}" if feed else "")


def latest_units(inc, known=()) -> list[str]:
    """The apparatus the latest page for this incident named (the monitor's incident keeps every page's units; the
    latest page says who is paged now: an upgrade to the full department names none). known: the configured
    apparatus (Scope.roster()), read as the station reads the page: "Engine 59/63" two units, a run-together
    "Engine 6158" split (sizeup/dispatch.py), so the line says what inc.units says."""
    texts = list(getattr(inc, "page_text", None) or [])
    if texts:
        from sizeup.dispatch import parse, split_joined
        return split_joined(parse(texts[-1], known or None).units, known) if known else list(parse(texts[-1]).units)
    return list(getattr(inc, "units", None) or [])


def dept_key(name: str) -> str:
    """A department as compared: 'Port Chester FD' / 'Port Chester · Fire' / 'Port Chester' -> 'port chester'."""
    n = base_name(str(name or "")).partition(" · ")[0]
    n = re.sub(r"\s+(?:fire\s+department|fire\s+dept|fire|fd)\s*$", "", n.strip(), flags=re.I)
    return re.sub(r"\s+", " ", n).strip().lower()


# A full-department dispatch said in words. "all units / companies / apparatus" only as the order to respond ("all units
# respond", "all units, please respond"): on its own it is also how advisories and clears start ("all units be advised",
# "all units clear", "all units responding, be advised" to whoever was sent), so it upgrades only with "respond" right after.
FULL_WORDS = re.compile(r"\bfull\s+(?:department|dept|response|assignment)\b|\bgeneral\s+alarm\b|\ball[\s-]+hands\b"
                        r"|\ball\s+(?:units|companies|apparatus)\s*,?\s*(?:please\s+)?respond\b", re.I)
# "full department" as speech-to-text hears it, by its consonants (fuzzy.skeleton's way; d as t, v as f): "depart",
# "the part", "dept", "of pots", "apartment". Read only right after a department's or town's name (said_full).
DEPT_SOUND = re.compile(r"[tf]pr?t(?:mnts?|s)?|pr?tmnts?")
FILLER = r"(?:\s+(?:fire|department|dept|fd))*"


def _consonants(words) -> str:
    s = skeleton("x" + "".join(re.sub(r"[^a-z]", "", w.lower()) for w in words))[1:]
    return re.sub(r"(.)\1+", r"\1", s.replace("d", "t").replace("v", "f"))


def said_full(text: str, names=()) -> bool:
    """A full-department dispatch said in a page's words: FULL_WORDS outright, or "full" and the one or two words
    after it sounding like "department" ("Exampletown full of pots") right after one of these departments' or towns'
    names (names: as the tones or the lists say them; their town part is what is matched)."""
    from sizeup.dispatch import _norm
    raw = str(text or "")
    t = _norm(raw)
    if FULL_WORDS.search(raw) or FULL_WORDS.search(t):
        return True
    for key in sorted({dept_key(n) for n in names or ()} - {""}):
        for m in re.finditer(rf"\b{re.escape(key)}{FILLER}[\s,.;:]+full\b[\s,]*((?:[a-z]+\s?){{1,2}})", t):
            words = m.group(1).split()
            if any(DEPT_SOUND.fullmatch(_consonants(words[:k])) for k in range(1, len(words) + 1)):
                return True
    return False


def upgrade(inc, seen_tones) -> bool:
    """Evidence that a page upgraded a call from specific units to its full department, beyond the latest reading
    naming no units (a repeat read again by speech-to-text can lose the names; the tones and type it shares are no
    evidence): tones of a department not heard before on this call (a new dispatch, its own tones); a different call
    type than the page before it; a full dispatch said (FULL_WORDS: "full department / response / assignment", "all
    units respond", "general alarm", "all hands"); or no units named in two readings in a row (one
    lossy reading is plausible, two independent ones much less)."""
    from sizeup.dispatch import parse
    tones = {base_name(t) for t in getattr(inc, "tone_departments", None) or () if t}
    if seen_tones and tones - set(seen_tones):
        return True
    texts = list(getattr(inc, "page_text", None) or [])
    if not texts:
        return False
    if said_full(texts[-1], [*tones, *(getattr(inc, "departments", None) or ())]):
        return True
    if len(texts) < 2:
        return False
    last, prev = parse(texts[-1]), parse(texts[-2])
    if "Unknown" not in (last.incident_type, prev.incident_type) and last.incident_type != prev.incident_type:
        return True
    return not last.units and not prev.units


class Scope:
    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        with self.store.lock:
            self.store.db.executescript(SCHEMA)
            have = {c[1] for c in self.store.db.execute("PRAGMA table_info(dispatch_scope)")}
            if "feed" not in have:                       # a table from before the page won (#370 review)
                self.store.db.execute("ALTER TABLE dispatch_scope ADD COLUMN feed TEXT NOT NULL DEFAULT ''")

    def roster(self) -> list[str]:
        """The units on the boards (their codes): the configured apparatus a page's units are read against."""
        try:
            return [u[0] for u in self.cad.lists.board_units()]
        except (AttributeError, TypeError):
            return []

    def on_page(self, iid: int | None, inc, note: bool = True) -> tuple[str, bool]:
        """A page (the monitor's incident: its tones, the apparatus it named, its type) made or reached this incident.
        (the feed's label as the timeline says it ("units") when it says otherwise than the page, else ""; whether this
        reading is new here). note: that disagreement is its own timeline line (once per reading; a page on a feed's
        incident says it in its one line instead: page_line)."""
        if not iid:
            return "", False
        tones, typ = getattr(inc, "tone_departments", ()), getattr(inc, "incident_type", "")
        units = latest_units(inc, self.roster())
        text = (list(getattr(inc, "page_text", None) or []) or [""])[-1]
        got = from_page(tones, units, typ, text)
        if got is None:
            return "", True                              # nothing to keep: the line says what it heard
        heard = list(dict.fromkeys([*(tones or []), *(getattr(inc, "departments", None) or [])]))
        seen = sorted({base_name(t) for t in tones or () if t})
        r = self.store.one("SELECT page FROM dispatch_scope WHERE incident_id = ?", (iid,))
        before = json.loads(r["page"]) if r is not None and r["page"] else None
        if before and before[0] == UNITS and got[0] == FULL and not upgrade(inc, before[3] if len(before) > 3 else []):
            # no evidence of an upgrade (a repeat that lost the names): the reading stands; its tones kept as seen
            got = (UNITS, list(before[1]))
            units = []
            line = before[2] if len(before) > 2 else ""
            return self._reading(iid, *got, note=note, line=line, tones=seen)
        return self._reading(iid, *got, note=note, line=page_line(heard, list(units or []), tones, typ, text=text),
                             tones=seen)

    @staticmethod
    def _feed_of(r) -> tuple[str, list] | None:
        """The feed's reading on a row (a row from before the feed column: its label when the feed set it)."""
        if r is None:
            return None
        return _parse(r["feed"]) or ((r["kind"], json.loads(r["departments"])) if r["source"] == "feed" else None)

    @staticmethod
    def _noted(db, iid: int, page: str, feed: str) -> bool:
        """This disagreement (the page's, the feed's, as the timeline says them) is on the timeline already, in either
        order: one line per disagreement."""
        return db.execute("SELECT 1 FROM events WHERE incident_id = ? AND text IN (?, ?) LIMIT 1",
                          (iid, f"Feed: {feed} · page: {page}", f"Page: {page} · feed: {feed}")).fetchone() is not None

    def _reading(self, iid: int, kind: str, depts: list[str], note: bool = True, line: str = "",
                 tones=None) -> tuple[str, bool]:
        """The page's reading onto the incident: it is the label from now on (#370 review: the page wins). (the feed's
        reading as the timeline says it when it differs and that is not on the timeline yet, else ""; whether the
        page's reading changed here)."""
        reading = json.dumps([kind, depts, line, list(tones or [])])
        now = self.store.clock()
        with self.store.tx() as db:
            r = db.execute("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,)).fetchone()
            feed = self._feed_of(r)
            said = _said(*feed) if _differ((kind, depts), feed) else ""
            if said and self._noted(db, iid, _said(kind, depts), said):
                said = ""                                 # said once already, whichever came first
            before = _parse(r["page"]) if r is not None else None
            if r is not None and r["page"] == reading:
                return said, False
            was = self._shown(r) if r is not None else None
            now_kind = shown((kind, depts), feed, tones)[1][0]
            db.execute("INSERT INTO dispatch_scope (incident_id, kind, departments, source, at, page, feed) "
                       "VALUES (?, ?, ?, 'page', ?, ?, ?) ON CONFLICT(incident_id) DO UPDATE SET kind = excluded.kind, "
                       "departments = excluded.departments, source = 'page', at = excluded.at, page = excluded.page, "
                       "feed = excluded.feed",
                       (iid, kind, json.dumps(depts), now, reading, json.dumps(list(feed)) if feed else ""))
            if note and before is not None and before != (kind, depts):
                self.store._event(db, iid, "feed", f"Page: {_said(*before)} → {_said(kind, depts)}", at=now)
            elif note and said and before is None:
                self.store._event(db, iid, "feed", f"Page: {_said(kind, depts)} · feed: {said}", at=now)
        if now_kind == FULL and (was is None or was[0] != FULL):
            self._widened(iid)
        elif now_kind != FULL and was is not None and was[0] == FULL:
            self._narrowed(iid)
        return said, True

    def _narrowed(self, iid: int) -> None:
        """Not a full department any more (a later page): what only widening linked is taken back (radiolog.py)."""
        try:
            self.cad.radiolog.narrow(iid)
        except Exception as e:  # noqa: BLE001 - the label stands whatever this does
            self.cad.log(f"CAD: dispatch scope: radio log: {type(e).__name__}")

    def line(self, iid: int) -> str:
        """The page's own line as the reading stands ("Page: all PCFD"), else ""."""
        r = self.store.one("SELECT page FROM dispatch_scope WHERE incident_id = ?", (iid,))
        x = json.loads(r["page"]) if r is not None and r["page"] else None
        return x[2] if x and len(x) > 2 else ""

    def paged(self, iid: int) -> list[str]:
        """The departments paged in full on this incident (as shown), else []."""
        r = self.store.one("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,))
        if r is None:
            return []
        kind, depts = self._shown(r)
        return list(depts) if kind == FULL else []

    def _widened(self, iid: int) -> None:
        """Full department now: the radio log's backfill again, every unit from the call's dispatch (radiolog.py)."""
        try:
            self.cad.radiolog.widen(iid)
        except Exception as e:  # noqa: BLE001 - the label stands whatever this does
            self.cad.log(f"CAD: dispatch scope: radio log: {type(e).__name__}")

    @classmethod
    def _pick(cls, r) -> tuple[str, tuple[str, list]]:
        """("page" | "feed", the reading) a row shows (shown(): the feed's department-level dispatch, else the
        page's, else the feed's; a row from before the feed column: its label)."""
        page = _parse(r["page"])
        try:
            tones = json.loads(r["page"])[3] if page is not None else []
        except (ValueError, IndexError, TypeError):
            tones = []
        who, got = shown(page, cls._feed_of(r), tones)
        return (who, got) if got is not None else (r["source"], (r["kind"], json.loads(r["departments"])))

    @classmethod
    def _shown(cls, r) -> tuple[str, list]:
        """What a row shows (_pick)."""
        return cls._pick(r)[1]

    def full(self, iid: int) -> bool:
        """Full department paged (as shown)."""
        r = self.store.one("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,))
        return r is not None and self._shown(r)[0] == FULL

    def move(self, src: int, dst: int) -> None:
        """A page's card moved onto another incident (Sources.rematch_page, _sweep_pages): its page's reading goes
        with it (it stands there too) and its one line, kind "tones", with " · feed: ..." when the feed
        says otherwise (once: never if that text is there already)."""
        r = self.store.one("SELECT page FROM dispatch_scope WHERE incident_id = ?", (src,))
        if r is None or not r["page"]:
            return
        kind, depts, *rest = json.loads(r["page"])
        line = rest[0] if rest else ""
        feed, _ = self._reading(dst, kind, list(depts), note=False, line=line, tones=rest[1] if len(rest) > 1 else [])
        text = (line + (f" · feed: {feed}" if feed else "")) if line else ""
        if not text:
            return
        with self.store.tx() as db:
            if not db.execute("SELECT 1 FROM events WHERE incident_id = ? AND kind = 'tones' AND text = ?", (dst, text)).fetchone():
                self.store._event(db, dst, "tones", text, "Paging", at=self.store.clock())

    def _placeholder_dept(self, code) -> str:
        """The department a placeholder code on a feed's unit list stands for ("department dispatched, no unit yet":
        Lists.placeholder, department_of), "" for any other code."""
        try:
            L = self.cad.lists
            return (L.department_of(str(code)) or "") if L.placeholder(str(code)) else ""
        except (AttributeError, TypeError):
            return ""

    def on_feed(self, iid: int | None) -> None:
        """A feed's report applied to this incident: its reading (from_units) kept beside the page's; which one is the
        label: shown(). A line says where the two differ (once per feed reading). A department-level dispatch stays
        one: its units signing on (the placeholder gone, apparatus listed) never make it specific units."""
        if not iid:
            return
        rep = self.store.one("SELECT data, label FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = '' "
                             "ORDER BY rank, first_seen LIMIT 1", (iid, PAGING))
        if rep is None:
            return
        data = json.loads(rep["data"] or "{}")
        got = from_units(data.get("units") or [], self.cad.sources.department_units(), self._placeholder_dept,
                         data.get("comments") or [])
        if got is None:
            return
        kind, depts = got
        now = self.store.clock()
        with self.store.tx() as db:
            r = db.execute("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,)).fetchone()
            had = self._feed_of(r)
            if had is not None and had[0] == FULL:
                if kind != FULL:
                    return
                depts = _merged(had[1], depts)
            if had == (kind, depts):
                return
            page = _parse(r["page"]) if r is not None else None
            was = self._shown(r) if r is not None else None
            if page is None:                              # no page heard: the feed's is the label
                db.execute("INSERT INTO dispatch_scope (incident_id, kind, departments, source, at, feed) "
                           "VALUES (?, ?, ?, 'feed', ?, ?) ON CONFLICT(incident_id) DO UPDATE SET kind = excluded.kind, "
                           "departments = excluded.departments, source = 'feed', at = excluded.at, feed = excluded.feed",
                           (iid, kind, json.dumps(depts), now, json.dumps([kind, depts])))
            else:
                db.execute("UPDATE dispatch_scope SET feed = ? WHERE incident_id = ?", (json.dumps([kind, depts]), iid))
                if _differ(page, (kind, depts)) and not self._noted(db, iid, _said(*page), _said(kind, depts)):
                    self.store._event(db, iid, "feed", f"Feed: {_said(kind, depts)} · page: {_said(*page)}", at=now)
            after = self._shown(db.execute("SELECT * FROM dispatch_scope WHERE incident_id = ?", (iid,)).fetchone())
        if after[0] == FULL and (was is None or was[0] != FULL):
            self._widened(iid)

    def decorate(self, incs: list[dict]) -> list[dict]:
        """inc["scope"] = {kind, departments, short, label, paged, source: page | feed (whose reading is shown), feed:
        the feed's reading as the timeline says it when the page's is shown and it differs, page: the page's when the
        feed's is shown and it differs (each else "")} or None."""
        ids = [i["id"] for i in incs]
        rows: dict[int, dict] = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for r in self.store.q(f"SELECT * FROM dispatch_scope WHERE incident_id IN ({','.join('?' * len(part))})", part):
                rows[r["incident_id"]] = r
        for inc in incs:
            r = rows.get(inc["id"])
            who, (kind, depts) = self._pick(r) if r else ("", ("", []))
            lab = label(kind, depts) if r else ""
            page, feed = (_parse(r["page"]), self._feed_of(r)) if r else (None, None)
            other = page if who == "feed" else feed          # the reading not shown, said when it differs
            differs = _said(*other) if _differ(page, feed) else ""
            inc["scope"] = {"kind": kind, "departments": depts, "short": short(kind, depts), "label": lab,
                            "paged": f"Paged: {lab[:1].lower()}{lab[1:]}", "source": who,
                            "feed": differs if who == "page" else "", "page": differs if who == "feed" else ""} if r else None
        return incs
