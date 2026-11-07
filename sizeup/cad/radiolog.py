"""The per-incident radio log (#311), in both dispatch modes: while an incident is open (from LEAD_S before it was made
until it closed) a transcribed transmission goes on it (radio_links, sizeup/cad/comms.py: the incident's radio card
plays the clip and shows the transcript) when

    mention     the transcript names a unit on the boards that is attached to it then (an assignment not yet cleared,
                or a unit its external CAD feed lists that is a board unit, matched exactly as automode does), or its
                address (house number and street together, as sizeup/cad/sources.py same_place reads them; an
                intersection: both streets with their suffixes, or "X and Y" / "X & Y"), or its named place (two words
                or more, not a common phrase in PLACE_STOP, as a whole phrase). why "mention", unit: the unit,
                "address" or "place"
    fireground  it is on a tactical channel (FG1) that is this incident's working fireground channel, when that is
                known: its external CAD feed names it (extra "fireground"), and no other open incident has it. Never
                guessed from who is talking (#163). why "fireground", unit: the channel

and a page for it already goes on it (comms.link_pages, why "page").

False-positive guards: an analog key-up from a radio nobody named is never linked by its words (#163: only a known
fireground channel takes it); only our dispatch and fire channels (the 60 Control feed's and the incident talkgroups,
the status-hint talkgroups, the tactical and paging channels; never the airband or anyone else's talkgroups); a unit
mention on a paging channel never counts (a page for a new call names units that may be busy elsewhere); a unit
mention in a transmission that sends it somewhere else ("respond to <a place that isn't this one>", or a numbered
address, with or without its suffix, that isn't this incident's) doesn't count; an address or place two open incidents
share links to neither; a unit counts only for the one incident it is on; a feed's unit code that isn't on the boards
never matches by text; more than MAX_PER_TX incidents for one transmission: none. A link a person removes (one click on
the incident's radio card) is never put back (radio_link_exclusions).

Retention: a mention or fireground link doesn't keep its audio or its row past the normal unlinked retention until a
person keeps it ("Keep" on the radio card: radio_link_kept, sizeup/cad/comms.py AUTO_UNSURE).
"""

from __future__ import annotations

import json
import re
import threading
import time

from sizeup.cad.comms import Comms
from sizeup.cad.feed import unit_forms
from sizeup.cad.sources import ABBR, INTERSECTION, NOISE, PAGING, SUFFIXES, _street, place_key, same_place, street_part

LEAD_S = 120.0
MAX_PER_TX = 3
BACKFILL_MAX = 300
BOARD_TTL_S = 30.0
WORD = re.compile(r"[a-z0-9]+")
NUM = re.compile(r"\d{1,5}[a-z]?")
UNIT_WORDS = {"engine", "ladder", "tower", "rescue", "truck", "utility", "marine", "squad", "medic", "ambulance", "alpha",
              "unit", "car", "chief", "e", "l", "tl", "r", "u", "m"}
# Named places too common in fire radio to mean this incident's place
PLACE_STOP = {"fire house", "firehouse", "station", "fire station", "headquarters", "quarters", "fire headquarters",
              "police station", "police headquarters", "hospital", "school", "high school", "middle school",
              "elementary school", "park", "town hall", "village hall", "city hall", "building", "house", "scene",
              "command post", "fire department", "police department", "post office", "train station", "gas station",
              "parking lot", "parking garage", "nursing home", "senior center", "shopping center", "medical center",
              "car wash", "apartment complex"}
RESPOND = re.compile(r"\brespond(?:ing)?\s+to\s+(?:the\s+)?(?P<where>[^.;,!?]+?)(?=\s+(?:for|on|with|reported|"
                     r"second|third|report)\b|[.;,!?]|$)", re.I)
TO_NUM = re.compile(r"\b(?:to|at)\s+(\d{1,5}[a-z]?\s+(?!(?:control|dispatch|minutes?|hours?|seconds?|units?|feet|ft|"
                    r"stor(?:y|ies)|floors?|alarms?|percent|degrees|engines?|ladders?|trucks?)\b)[a-z]+(?:\s+[a-z]+)?)", re.I)


def _key(s: str) -> str:
    return re.sub(r"[\s-]+", "", str(s or "")).upper()


def _tokens(s: str) -> list[str]:
    return [ABBR.get(w, w) for w in WORD.findall((s or "").lower().replace("&", " and "))]


def _seq(toks: list[str], run: list[str]) -> list[int]:
    """The start indexes where run appears in toks, contiguous."""
    n = len(run)
    return [i for i in range(len(toks) - n + 1) if run and toks[i:i + n] == run]


def _ordered(part: str) -> tuple[list[str], str]:
    """A street's name words in order, and its suffix: 'N Spring St' -> (['spring'], 'st')."""
    _, pre, suffix, name, _post = _street(part)
    words = [w for w in _tokens(part) if w in name]
    return list(dict.fromkeys(words)) or sorted(name), suffix


def address_hit(text: str, address: str) -> bool:
    """The transcript says this address: '12 Example Avenue' for '12 Example Ave', '12 North Street' for '12 North St';
    an intersection only with both streets' suffixes ('Spring Street ... Main Street') or as 'Spring and Main'."""
    s = street_part(address)
    k = place_key(s)
    if k is None:
        return False
    toks = _tokens(text)
    if k[0] == "x":
        parts = [p for p in INTERSECTION.split(s) if p.strip()]
        if len(parts) != 2:
            return False
        (a, sa), (b, sb) = (_ordered(p) for p in parts)
        if not a or not b:
            return False
        if sa and sb and _seq(toks, a + [sa]) and _seq(toks, b + [sb]):
            return True
        for x, sx, y in ((a, sa, b), (b, sb, a)):          # "Spring and Main", "Spring St & Main"
            for i in _seq(toks, x):
                j = i + len(x)
                if j < len(toks) and toks[j] == sx:
                    j += 1
                if j < len(toks) and toks[j] == "and" and toks[j + 1:j + 1 + len(y)] == y:
                    return True
        return False
    if k[0] != "addr" or not k[1]:
        return False                                     # a street alone: too common to count
    for i, t in enumerate(toks):
        if t != k[1]:
            continue
        for j in range(i + 2, min(i + 7, len(toks) + 1)):
            if same_place(" ".join(toks[i:j]), s):
                return True
    return False


def place_hit(text: str, place: str) -> bool:
    """The transcript names this place as a whole phrase: two words or more, never a common one (PLACE_STOP)."""
    words = [w for w in WORD.findall((place or "").lower()) if w not in NOISE]
    if len(words) < 2 or " ".join(words) in PLACE_STOP:  # "the" aside: "The Fire House" is "fire house"
        return False
    return re.search(r"(?<![\w])" + r"\W+(?:the\W+)?".join(re.escape(w) for w in words) + r"(?![\w])",
                     (text or "").lower()) is not None


def addresses_in(text: str) -> list[str]:
    """Address-like phrases in a transcript: a number then a street with its suffix within 4 words ('400 Sample St'),
    and a number after "to" / "at" with a word or two, suffix or not ('respond to 400 Sample')."""
    toks = _tokens(text)
    out = []
    for i, t in enumerate(toks):
        if not NUM.fullmatch(t) or (i and toks[i - 1] in UNIT_WORDS):
            continue                                     # "Engine 60 on Example Ave": a unit, not a house number
        for j in range(i + 2, min(i + 6, len(toks))):
            if re.search(r"\d", toks[j - 1]) and j - 1 > i:
                break
            if toks[j] in SUFFIXES:
                out.append(" ".join(toks[i:j + 1]))
                break
    for m in TO_NUM.finditer(text or ""):
        toks2 = _tokens(m.group(1))
        frag = " ".join(toks2[:3] if len(toks2) > 2 and toks2[2] in SUFFIXES else toks2[:2])
        if not any(o.startswith(frag) or frag.startswith(o) for o in out):
            out.append(frag)
    return out


def sent_elsewhere(text: str, inc: dict) -> bool:
    """The transmission sends someone to another place than this incident's: "respond to <somewhere else>", or a
    numbered address (with or without its suffix) that isn't this incident's."""
    mine = [x for x in (inc["address"], inc["place_address"]) if x]

    def here(frag: str) -> bool:
        return any(address_hit(frag, a) or same_place(frag, street_part(a)) for a in mine) or \
            bool(inc.get("place")) and place_hit(frag, inc["place"])
    for frag in addresses_in(text):
        if not here(frag):
            return True
    for m in RESPOND.finditer(text or ""):
        where = m.group("where").strip()
        if where and not here(where) and not here(text[m.start():m.end()]):
            return True
    return False


class RadioLog:
    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        self.inline = False                              # tests: the backfill runs on the caller's thread
        self._board: tuple[float, dict] | None = None
        self._rx: dict[tuple[str, str], re.Pattern] = {}

    def _channels(self) -> tuple[set, set]:
        """(talkgroups, FM channels) our dispatch and fire traffic is on."""
        c = self.cad
        tgs = set(c.s.incident_talkgroups)
        try:
            c.feed._refresh()
            tgs |= set(c.feed.cfg.talkgroups)
        except Exception:  # noqa: BLE001
            pass
        try:
            c.hints._refresh()
            tgs |= set(c.hints.cfg.talkgroups)
            fm = set(c.hints.cfg.channels)
        except Exception:  # noqa: BLE001
            fm = set()
        fm |= set(c.s.tactical) | set(c.s.paging)
        return tgs, fm - set(c.s.airband)

    def board(self) -> dict[str, tuple[str, str]]:
        """A unit code as written (spaces, dashes and case aside) -> (board code, spoken), for every board unit; cached."""
        now = time.monotonic()
        if self._board is None or now - self._board[0] > BOARD_TTL_S:
            out: dict[str, tuple[str, str]] = {}
            for u in self.store.q("SELECT name, spoken FROM units WHERE board != ''"):
                for k in (_key(u["name"]), _key(u["spoken"])):
                    if k:
                        out.setdefault(k, (u["name"], u["spoken"]))
            self._board = (now, out)
        return self._board[1]

    def _unit_rx(self, code: str, spoken: str) -> re.Pattern:
        k = (code, spoken)
        if k not in self._rx:
            self._rx[k] = re.compile(r"(?<![\w])(?:" + "|".join(unit_forms(code, spoken)) + r")(?![\w])", re.I)
        return self._rx[k]

    def context(self, lo: float, hi: float) -> dict:
        """What matching needs, read once: the channels, the board, the incidents open between lo and hi with their
        assignments and their feeds' units and fireground."""
        tgs, fm = self._channels()
        incs = self.store.q("SELECT * FROM incidents WHERE status != 'dismissed' AND created_at <= ? "
                            "AND (closed_at IS NULL OR closed_at >= ?) ORDER BY id", (hi + LEAD_S, lo))
        ids = [i["id"] for i in incs]
        assigns: dict[int, list] = {}
        feeds: dict[int, list] = {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            marks = ",".join("?" * len(part))
            for r in self.store.q(f"SELECT a.incident_id, a.assigned_at, a.cleared_at, u.name FROM assignments a JOIN units u "
                                  f"ON u.id = a.unit_id WHERE a.incident_id IN ({marks}) AND a.assigned_at IS NOT NULL", part):
                assigns.setdefault(r["incident_id"], []).append(r)
            for r in self.store.q(f"SELECT incident_id, data FROM source_reports WHERE incident_id IN ({marks}) AND source != ? "
                                  f"AND merged_into = '' ORDER BY rank, first_seen", (*part, PAGING)):
                feeds.setdefault(r["incident_id"], []).append(json.loads(r["data"] or "{}"))
        return {"tgs": tgs, "fm": fm, "paging": set(self.cad.s.paging), "tactical": set(self.cad.s.tactical),
                "board": self.board(), "incs": incs, "assigns": assigns, "feeds": feeds}

    def fireground(self, iid: int, ctx: dict | None = None) -> str:
        """The incident's working fireground channel when known: its external CAD feed says it (extra "fireground", a
        channel label such as "FG1"); '' when not known (never guessed from who is talking: #163)."""
        feeds = (ctx or {}).get("feeds", {}).get(iid) if ctx else [
            json.loads(r["data"] or "{}") for r in self.store.q(
                "SELECT data FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = '' "
                "ORDER BY rank, first_seen", (iid, PAGING))]
        for d in feeds or []:
            v = (d.get("extra") or {}).get("fireground")
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _units_on(self, ctx: dict, iid: int, at: float) -> set[str]:
        """The board units attached to the incident at that time: its assignments (from LEAD_S before put on, until
        cleared) and the units its feed lists that are board units (exactly)."""
        out = {r["name"] for r in ctx["assigns"].get(iid, [])
               if r["assigned_at"] <= at + LEAD_S and (r["cleared_at"] is None or r["cleared_at"] >= at)}
        for d in ctx["feeds"].get(iid, []):
            for u in d.get("units") or []:
                b = ctx["board"].get(_key(u))
                if b:
                    out.add(b[0])
        return out

    def matches(self, t: dict, ctx: dict | None = None) -> dict[int, tuple[str, str]]:
        """incident id -> (why, unit) for this transmission (see the module's rules)."""
        ctx = ctx or self.context(t["start"], t["start"])
        if t["talkgroup"] is not None:
            if t["talkgroup"] not in ctx["tgs"]:
                return {}
            paging = False
        else:
            if t["channel"] not in ctx["fm"]:
                return {}
            paging = t["channel"] in ctx["paging"]
        incs = [i for i in ctx["incs"] if i["created_at"] <= t["start"] + LEAD_S
                and (i["closed_at"] is None or i["closed_at"] >= t["start"])]
        if not incs:
            return {}
        out: dict[int, tuple[str, str]] = {}
        text = (t["text"] or "").strip()
        # an analog key-up from a radio nobody named (no ID, or an ID not in the registry) never goes on a call by what
        # it says (#163: "only add them if we know it"); only a known fireground channel takes it
        unknown = t["talkgroup"] is None and not paging and not t["rig"]
        if text and not unknown:
            by_place = [i["id"] for i in incs if any(address_hit(text, a) for a in (i["address"], i["place_address"]) if a)]
            what = "address"
            if not by_place:
                by_place = [i["id"] for i in incs if i["place"] and place_hit(text, i["place"])]
                what = "place"
            if len(by_place) == 1:
                out[by_place[0]] = ("mention", what)
            if not paging:
                owner: dict[str, list[int]] = {}
                for i in incs:
                    for u in self._units_on(ctx, i["id"], t["start"]):
                        owner.setdefault(u, []).append(i["id"])
                spoken = {code: sp for code, sp in ctx["board"].values()}
                for u, ids in sorted(owner.items()):
                    if len(set(ids)) != 1 or ids[0] in out:
                        continue
                    iid = ids[0]
                    if not self._unit_rx(u, spoken.get(u, "")).search(text):
                        continue
                    if sent_elsewhere(text, next(i for i in incs if i["id"] == iid)):
                        continue                         # dispatch sending it somewhere else
                    out[iid] = ("mention", u)
        if t["talkgroup"] is None and t["channel"] in ctx["tactical"]:
            fg = [i["id"] for i in incs if i["status"] in ("pending", "active") and self.fireground(i["id"], ctx) == t["channel"]]
            if len(fg) == 1:
                out.setdefault(fg[0], ("fireground", t["channel"]))
        return out if len(out) <= MAX_PER_TX else {}     # more than that: it isn't about one call

    def _link(self, tid: str, found: dict[int, tuple[str, str]]) -> list[int]:
        got = []
        with self.store.tx() as db:
            have = {r[0] for r in db.execute("SELECT incident_id FROM radio_links WHERE traffic_id = ?", (tid,))}
            for iid, (why, unit) in found.items():
                if iid in have:
                    continue
                before = db.total_changes
                Comms._auto(db, tid, iid, why, unit)
                if db.total_changes != before:
                    got.append(iid)
        if got:
            self.cad.comms.rev += 1
        return got

    def consider(self, tid: str) -> list[int]:
        """A transcript arrived (or changed): the incidents it went on now."""
        t = self.store.one("SELECT * FROM radio_traffic WHERE id = ?", (tid,))
        if t is None:
            return []
        return self._link(tid, self.matches(t))

    def backfill(self, iid: int) -> list[str]:
        """An incident just reported: the transmissions since LEAD_S before it was made, matched (only to it). The
        channels, board and open incidents are read once for them all."""
        inc = self.store.incident(iid)
        lo, hi = inc["created_at"] - LEAD_S, self.cad.clock() + 1
        rows = self.store.q("SELECT * FROM radio_traffic WHERE start >= ? AND start <= ? ORDER BY start LIMIT ?",
                            (lo, hi, BACKFILL_MAX))
        if not rows:
            return []
        if len(rows) >= BACKFILL_MAX:
            self.cad.log(f"CAD: radio log backfill for event {iid}: the first {BACKFILL_MAX} transmissions only")
        ctx = self.context(lo, hi)
        out = []
        for t in rows:
            f = self.matches(t, ctx)
            if iid in f and self._link(t["id"], {iid: f[iid]}):
                out.append(t["id"])
        return out

    def backfill_soon(self, iid: int) -> None:
        """The backfill off the feed's thread (never under the dispatch mode's lock); inline for tests."""
        def run():
            try:
                self.backfill(iid)
            except Exception as e:  # noqa: BLE001
                self.cad.log(f"CAD: radio log backfill: {type(e).__name__}: {e}")
        if self.inline:
            run()
        else:
            threading.Thread(target=run, name="radiolog-backfill", daemon=True).start()

    def keep(self, tid: str, iid: int, operator: str = "", undo: bool = False) -> dict:
        """A person confirms an automatic mention / fireground link (its audio and row then kept like the event's), or
        takes that back (undo): a line in the event's timeline either way."""
        from sizeup.cad.comms import _when, _who
        with self.store.tx() as db:
            t = db.execute("SELECT * FROM radio_traffic WHERE id = ?", (tid,)).fetchone()
            if t is None or not db.execute("SELECT 1 FROM radio_links WHERE traffic_id = ? AND incident_id = ?",
                                           (tid, iid)).fetchone():
                return {"kept": False, "error": "not on this event"}
            if undo:
                n = db.execute("DELETE FROM radio_link_kept WHERE traffic_id = ? AND incident_id = ?", (tid, iid)).rowcount
            else:
                n = db.execute("INSERT OR IGNORE INTO radio_link_kept (traffic_id, incident_id, at, operator) VALUES (?, ?, ?, ?)",
                               (tid, iid, self.cad.clock(), operator)).rowcount
            if n:
                self.store._event(db, iid, "radio", f"Radio {_when(t)} {_who(t)} on {t['channel']} "
                                  + ("no longer kept" if undo else "kept with this event"), operator)
        self.cad.comms.rev += 1
        return {"kept": not undo}
