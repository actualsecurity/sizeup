"""The per-incident radio log (#311), in both dispatch modes: while an incident is open (from LEAD_S before it was made
until it closed) a transcribed transmission goes on it (radio_links, sizeup/cad/comms.py: the incident's radio card
plays the clip and shows the transcript) when

    mention     the transcript names a unit attached to it then (an assignment not yet cleared, or a unit its external
                CAD feed lists), or its address (the house number and the street together; an intersection: both
                streets) or its named place (two words or more). why "mention", unit: the unit, "address" or "place"
    fireground  it is on a tactical channel (FG1) that is this incident's working fireground channel, when that is
                known: its external CAD feed names it (extra "fireground"), and no other open incident has it. Never
                guessed from who is talking (#163). why "fireground", unit: the channel

and a page for it already goes on it (comms.link_pages, why "page").

False-positive guards: an analog key-up from a radio nobody named is never linked by its words (#163: only a known fireground
channel takes it); only our dispatch and fire channels (the 60 Control feed's and the incident talkgroups, the
status-hint talkgroups, the tactical and paging channels; never the airband or anyone else's talkgroups); a unit
mention on a paging channel never counts (a page for a new call names units that may be busy elsewhere); a unit
mention in a transmission that gives another address (a number and a street that are not this incident's) doesn't
count (dispatch sending that unit somewhere else); an address or place two open incidents share links to neither; a
unit counts only for the one incident it is on; at most MAX_PER_TX incidents per transmission. A link a person
removes (one click on the incident's radio card) is never put back (radio_link_exclusions).
"""

from __future__ import annotations

import json
import re

from sizeup.cad.comms import Comms
from sizeup.cad.feed import unit_forms
from sizeup.cad.sources import ABBR, DIRECTIONS, INTERSECTION, NOISE, PAGING, SUFFIXES, UNIT_TAIL, norm_unit, place_key, same_place

LEAD_S = 120.0
MAX_PER_TX = 3
BACKFILL_MAX = 300
WORD = re.compile(r"[a-z0-9]+")
UNIT_WORDS = {"engine", "ladder", "tower", "rescue", "truck", "utility", "marine", "squad", "medic", "ambulance", "alpha",
              "unit", "car", "chief", "e", "l", "tl", "r", "u", "m"}


def _tokens(s: str) -> list[str]:
    return [ABBR.get(w, w) for w in WORD.findall((s or "").lower())]


def _clean(s: str) -> str:
    s = (s or "").split(",")[0]
    s = s.split(" @ ")[0] if " @ " in s else s
    return UNIT_TAIL.sub("", s).strip()


def _name(words: list[str]) -> list[str]:
    return [w for w in words if w not in DIRECTIONS and w not in SUFFIXES and w not in NOISE]


def _has_run(tokens: list[str], run: list[str], start: int = 0, end: int | None = None) -> int:
    """The index after the first place run appears in tokens[start:end] (allowing a direction or suffix between its
    words), -1 when it doesn't."""
    end = len(tokens) if end is None else min(end, len(tokens))
    for i in range(start, end):
        k, j = 0, i
        while j < len(tokens) and k < len(run):
            if tokens[j] == run[k]:
                k += 1
            elif k == 0 or (tokens[j] not in DIRECTIONS and tokens[j] not in SUFFIXES):
                break
            j += 1
        if k == len(run):
            return j
    return -1


def address_hit(text: str, address: str) -> bool:
    """The transcript says this address: '12 Example Avenue' for '12 Example Ave'; both streets of an intersection."""
    s = _clean(address)
    if not s:
        return False
    toks = _tokens(text)
    parts = [p for p in INTERSECTION.split(s) if p.strip()]
    if len(parts) >= 2:
        names = [_name(_tokens(p)) for p in parts]
        return all(n and _has_run(toks, n) >= 0 for n in names)
    words = _tokens(s)
    if not words or not re.fullmatch(r"\d+[a-z]?", words[0]):
        return False                                     # a street alone: too common to count
    num, name = words[0], _name(words[1:])
    if not name:
        return False
    return any(t == num and _has_run(toks, name, i + 1, i + 3) >= 0 for i, t in enumerate(toks))


def place_hit(text: str, place: str) -> bool:
    words = [w for w in WORD.findall((place or "").lower()) if w not in NOISE]
    if len(words) < 2:
        return False
    return re.search(r"\b" + r"\W+".join(re.escape(w) for w in words) + r"\b", (text or "").lower()) is not None


def addresses_in(text: str) -> list[str]:
    """Address-like phrases in a transcript: a number then a street with its suffix within 4 words ('400 Sample St')."""
    toks = _tokens(text)
    out = []
    for i, t in enumerate(toks):
        if not re.fullmatch(r"\d{1,5}[a-z]?", t) or (i and toks[i - 1] in UNIT_WORDS):
            continue                                     # "Engine 60 on Example Ave": a unit, not a house number
        for j in range(i + 2, min(i + 6, len(toks))):
            if re.search(r"\d", toks[j - 1]) and j - 1 > i:
                break
            if toks[j] in SUFFIXES:
                out.append(" ".join(toks[i:j + 1]))
                break
    return out


class RadioLog:
    def __init__(self, cad):
        self.cad, self.store = cad, cad.store

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

    def _units_on(self, iid: int, at: float) -> set[str]:
        """The units attached to the incident at that time: its assignments (from LEAD_S before put on, until cleared)
        and the units its feed lists."""
        out = {r["name"] for r in self.store.q(
            "SELECT u.name FROM assignments a JOIN units u ON u.id = a.unit_id WHERE a.incident_id = ? AND "
            "a.assigned_at IS NOT NULL AND a.assigned_at <= ? AND (a.cleared_at IS NULL OR a.cleared_at >= ?)",
            (iid, at + LEAD_S, at))}
        for r in self.store.q("SELECT data FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = ''",
                              (iid, PAGING)):
            out |= {u for u in (json.loads(r["data"] or "{}").get("units") or []) if u}
        return out

    def _open_at(self, at: float) -> list[dict]:
        return self.store.q("SELECT * FROM incidents WHERE status != 'dismissed' AND created_at <= ? "
                            "AND (closed_at IS NULL OR closed_at >= ?) ORDER BY id", (at + LEAD_S, at))

    def _board(self) -> dict[str, tuple[str, str]]:
        """norm_unit -> (board code, spoken) for every unit on the boards."""
        out: dict[str, tuple[str, str]] = {}
        for u in self.store.units():
            for k in (norm_unit(u["name"]), norm_unit(u["spoken"])):
                if k:
                    out.setdefault(k, (u["name"], u["spoken"]))
        return out

    def matches(self, t: dict, incs: list[dict] | None = None) -> dict[int, tuple[str, str]]:
        """incident id -> (why, unit) for this transmission (see the module's rules)."""
        tgs, fm = self._channels()
        if t["talkgroup"] is not None:
            if t["talkgroup"] not in tgs:
                return {}
            paging = False
        else:
            if t["channel"] not in fm:
                return {}
            paging = t["channel"] in set(self.cad.s.paging)
        incs = self._open_at(t["start"]) if incs is None else incs
        if not incs:
            return {}
        out: dict[int, tuple[str, str]] = {}
        text = (t["text"] or "").strip()
        # an analog key-up from a radio nobody named (no ID, or an ID not in the registry) never goes on a call by what it
        # says (#163: "only add them if we know it"); only a known fireground channel takes it
        unknown = t["talkgroup"] is None and not paging and not t["rig"]
        if text and not unknown:
            said = addresses_in(text)
            # an address or place: one incident only
            by_place = [i["id"] for i in incs if any(address_hit(text, a) for a in (i["address"], i["place_address"]) if a)]
            what = "address"
            if not by_place:
                by_place = [i["id"] for i in incs if i["place"] and place_hit(text, i["place"])]
                what = "place"
            if len(by_place) == 1:
                out[by_place[0]] = ("mention", what)
            if not paging:
                board = self._board()
                owner: dict[str, list[int]] = {}
                for i in incs:
                    for u in {norm_unit(x) or x for x in self._units_on(i["id"], t["start"])}:
                        owner.setdefault(u, []).append(i["id"])
                for u, ids in sorted(owner.items()):
                    if len(set(ids)) != 1 or ids[0] in out:
                        continue
                    iid = ids[0]
                    inc = next(i for i in incs if i["id"] == iid)
                    if said and not any(same_place(a, x) for a in said for x in (inc["address"], inc["place_address"]) if x):
                        continue                         # sent somewhere else
                    code, spoken = board.get(u, (u, ""))
                    if re.search(r"(?<![\w])(?:" + "|".join(unit_forms(code, spoken)) + r")(?![\w])", text, re.I):
                        out[iid] = ("mention", code)
        if t["talkgroup"] is None and t["channel"] in set(self.cad.s.tactical):
            fg = [i["id"] for i in incs if i["status"] in ("pending", "active") and self.fireground(i["id"]) == t["channel"]]
            if len(fg) == 1:
                out.setdefault(fg[0], ("fireground", t["channel"]))
        return dict(list(out.items())[:MAX_PER_TX])

    def fireground(self, iid: int) -> str:
        """The incident's working fireground channel when known: its external CAD feed says it (extra "fireground",
        a channel label such as "FG1"); '' when not known (never guessed from who is talking: #163)."""
        for r in self.store.q("SELECT data FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = '' "
                              "ORDER BY rank, first_seen", (iid, PAGING)):
            v = (json.loads(r["data"] or "{}").get("extra") or {}).get("fireground")
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

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
        """An incident just reported: the transmissions since LEAD_S before it was made, matched (only to it)."""
        inc = self.store.incident(iid)
        rows = self.store.q("SELECT * FROM radio_traffic WHERE start >= ? AND start <= ? ORDER BY start LIMIT ?",
                            (inc["created_at"] - LEAD_S, self.cad.clock() + 1, BACKFILL_MAX))
        out = []
        for t in rows:
            f = self.matches(t)
            if iid in f and self._link(t["id"], {iid: f[iid]}):
                out.append(t["id"])
        return out
