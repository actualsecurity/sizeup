"""The pinned "60 Control · Fire/EMS 15" feed on the CAD console: the last few transmissions on the
county's incident talkgroup(s), so the desk always sees what 60 Control said, and which of them
were "talking to us" (Port Chester / Rye Brook); and every page on the paging channel(s) (South 2,
#80), each saying what it was (rows[i]["page"], sizeup/cad/comms.py page_kind): "ours" (PCFD's or Rye
Brook's tones: the event it opened is on the row, pc = the department), "other" (another department's
tones), "unknown" (tones nobody's code matches: an admin names them from the row), "none" (no tones
decoded). Only an "ours" page is an event to accept; the rest stay visible so nothing is lost.

Settings come from the data file (data/cad_lists.json, "comms_feed"):

    title        the panel's title ("60 Control · Fire/EMS 15")
    talkgroups   P25 talkgroups shown (default [307] Fire/EMS 15; add 333 for an Airport incident)
    limit        how many transmissions (default 6)
    phrases      words that mean a transmission is about us ("Port Chester", "PCFD", "headquarters" ...)

A transmission is "talking to us" (rows[i]["pc"] says why) when its transcript or talker names a
phrase, or one of PCFD's or Rye Brook's units by code or as said ("E60", "Engine 60", "23 91"),
or it is 60 Control's turn right before or after such a unit's turn in the same P25 call (looking past
the key-ups of unknown radios in between).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sizeup.cad.comms import _tones, events_of
from sizeup.cad.lists import Lists, ListsError
from sizeup.cad.radios import PCFD_KINDS

DEFAULT_TITLE = "60 Control · Fire/EMS 15"
DEFAULT_TGS = (307,)
DEFAULT_LIMIT = 6
MAX_LIMIT = 50
DEFAULT_PHRASES = ("Port Chester", "Rye Brook", "PC dispatch", "Port Chester dispatch", "PCFD", "headquarters")


def _phrase_re(p: str) -> str:
    """'Port Chester' -> Port[\\s-]*Chester ('Portchester', 'Port-Chester'); an acronym 'PCFD' also
    matches 'P.C.F.D.' / 'P C F D'."""
    parts = []
    for w in p.split():
        if w.isupper() and w.isalpha() and len(w) <= 5:
            parts.append(r"[.\s]?".join(re.escape(ch) for ch in w) + r"\.?")
        else:
            parts.append(re.escape(w))
    return r"[\s-]*".join(parts)


def _number_re(n: str) -> str:
    """'60' -> 6[\\s-]?0 ('6-0', '6 0'); '2391' -> 23[\\s-]?91 too ('23-91', '23 91', '2391')."""
    return r"[\s-]?".join(re.escape(d) for d in n) if len(n) <= 2 else re.escape(n[:2]) + r"[\s-]?" + re.escape(n[2:])


def unit_forms(code: str, spoken: str = "") -> list[str]:
    """Regex alternatives for a unit as it is written or said: 'E60' / 'E-60' / 'Engine 60' / 'Engine 6-0';
    '2391' / '23 91' / '23-91'; 'TL2' / 'Tower Ladder 2'."""
    out = []
    m = re.fullmatch(r"([A-Z]+)(\d+)", code)
    if m:
        out.append(re.escape(m[1]) + r"[\s-]?" + _number_re(m[2]))
    elif code.isdigit():
        out.append(_number_re(code))
    else:
        out.append(re.escape(code))
    sp = (spoken or "").strip()
    if sp and sp != code:
        words = sp.split()
        if all(w.isdigit() for w in words):                 # "23 91"
            out.append(_number_re("".join(words)))
        else:
            out.append(r"[\s-]+".join(_number_re(w) if w.isdigit() else re.escape(w) for w in words))
    return list(dict.fromkeys(out))


@dataclass
class FeedCfg:
    title: str = DEFAULT_TITLE
    talkgroups: tuple[int, ...] = DEFAULT_TGS
    limit: int = DEFAULT_LIMIT
    phrases: tuple[str, ...] = DEFAULT_PHRASES
    units: dict[str, str] = field(default_factory=dict)      # code -> spoken: PCFD's and Rye Brook's units


def load_cfg(lists: Lists) -> FeedCfg:
    """"comms_feed" from the data file (missing: the defaults). ListsError on a bad value."""
    where = lists.path or "cad_lists"
    try:
        d = json.loads(Path(lists.path).read_text()) if lists.path else {}
    except (OSError, ValueError):
        d = {}
    f = d.get("comms_feed") or {}
    if not isinstance(f, dict):
        raise ListsError(f"{where}: comms_feed: an object {{title, talkgroups, limit, phrases}}")
    tgs = f.get("talkgroups", list(DEFAULT_TGS))
    if (not isinstance(tgs, list) or not tgs
            or not all(isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= 65535 for t in tgs)):
        raise ListsError(f"{where}: comms_feed.talkgroups: a list of P25 talkgroup numbers, e.g. [307, 333]")
    limit = f.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ListsError(f"{where}: comms_feed.limit: a number from 1 to {MAX_LIMIT}")
    phrases = f.get("phrases", list(DEFAULT_PHRASES))
    if not isinstance(phrases, list) or not all(isinstance(p, str) and p.strip() for p in phrases):
        raise ListsError(f"{where}: comms_feed.phrases: a list of words or phrases")
    title = f.get("title", DEFAULT_TITLE)
    if not isinstance(title, str) or not title.strip():
        raise ListsError(f"{where}: comms_feed.title: text")
    joint = set(lists.joint_response)
    units = {c: str(u.get("spoken") or "") for c, u in lists.units.items()
             if u.get("kind", "agency") in PCFD_KINDS and not u.get("mutual_aid")
             and (not u.get("department") or u.get("department") in joint)}
    return FeedCfg(title.strip(), tuple(dict.fromkeys(tgs)), limit, tuple(p.strip() for p in phrases), units)


class Matcher:
    """Is a transmission about us? match(text, talker, rig) -> the reason ('Port Chester', 'E60'), or ''."""

    def __init__(self, phrases: tuple[str, ...] | list[str], units: dict[str, str]):
        self.phrases = [(p, re.compile(r"(?<![\w])" + _phrase_re(p) + r"(?![\w])", re.I)) for p in phrases]
        self.units = [(c, re.compile(r"(?<![\w])(?:" + "|".join(unit_forms(c, sp)) + r")(?![\w])", re.I))
                      for c, sp in units.items()]
        self.codes = set(units)

    def match(self, text: str = "", talker: str = "", rig: str = "") -> str:
        for r in (x.strip() for x in (rig or "").split(",")):
            if r in self.codes:
                return r
        hay = f"{talker or ''}\n{text or ''}"
        for p, rx in self.phrases:
            if rx.search(hay):
                return p
        found = [(m.start(), c) for c, rx in self.units if (m := rx.search(hay))]
        return min(found)[1] if found else ""          # the first one named


class Feed:
    def __init__(self, store, lists, has_clip=lambda cid: False, paging: tuple[str, ...] = ("South 2",)):
        """lists: the Lists, or a function returning the current ones (Settings edits replace them:
        the units matched follow). paging: the channels whose rows are pages."""
        self.store, self.has_clip, self.paging = store, has_clip, tuple(paging)
        self._lists = lists if callable(lists) else (lambda: lists)
        self._for = None
        self._refresh()

    def _refresh(self) -> None:
        L = self._lists()
        if L is not self._for:
            self.cfg = load_cfg(L)
            self.matcher = Matcher(self.cfg.phrases, self.cfg.units)
            self._for = L

    def rows(self, limit: int | None = None, talkgroups: tuple[int, ...] | None = None) -> list[dict]:
        """The newest transmissions on the feed's talkgroups, newest first, each with pc (why it is
        talking to us, '' if not), has_audio and the events it is linked to."""
        self._refresh()
        tgs = tuple(talkgroups or self.cfg.talkgroups)
        n = max(1, min(int(limit or self.cfg.limit), MAX_LIMIT))
        pg = self.paging
        rows = self.store.q(f"SELECT * FROM radio_traffic WHERE talkgroup IN ({','.join('?' * len(tgs))}) "
                            + (f"OR (talkgroup IS NULL AND channel IN ({','.join('?' * len(pg))})) " if pg else "")
                            + "ORDER BY start DESC, turn DESC LIMIT ?", (*tgs, *pg, n))
        if not rows:
            return rows
        calls = sorted({r["call_id"] for r in rows if r["call_id"]})
        turns: dict[tuple[str, int], dict] = {}
        if calls:
            for s in self.store.q(f"SELECT id, call_id, turn, talker, rig, radio_id, dispatcher, text FROM radio_traffic "
                                  f"WHERE call_id IN ({','.join('?' * len(calls))})", calls):
                if s["turn"] is not None:
                    turns[(s["call_id"], s["turn"])] = s
        ev = events_of(self.store, [r["id"] for r in rows])
        for r in rows:
            _tones(r, pg)
            r["pc"] = self.page_pc(r) if r["page"] else self.pc(r, turns)
            r["events"] = ev.get(r["id"], [])
            r["has_audio"] = self.has_clip(r["id"])
        return rows

    def page_pc(self, r: dict) -> str:
        """Why a page is to us: its tones' department(s) when they are ours; '' for any other page, whatever
        its text says ("Port Chester" on a Fairview page is mutual aid, not an event), so the accent means
        one thing in the feed: an event of ours."""
        if r["page"] == "ours":
            return ", ".join(dict.fromkeys(n for t in r["tones"] if t.get("ours") for n in t.get("names") or []))
        return ""

    def pc(self, r: dict, turns: dict[tuple[str, int], dict]) -> str:
        why = self.matcher.match(r["text"], r["talker"], r["rig"])
        if why or not r["dispatcher"] or not r["call_id"] or r["turn"] is None:
            return why
        for step in (-1, 1):                                  # 60 Control answering (or calling) one of ours
            k = r["turn"] + step
            s = turns.get((r["call_id"], k))
            while s and not (s["talker"] or s["radio_id"] or s["rig"]):   # an unknown radio's key-up: look past it
                k += step
                s = turns.get((r["call_id"], k))
            if s and not s["dispatcher"]:
                w = self.matcher.match(s["text"], s["talker"], s["rig"])
                if w:
                    return f"answering {w}" if k < r["turn"] else f"calling {w}"
        return ""

    def state(self) -> dict:
        rows = self.rows()
        tg = [r for r in rows if r["talkgroup"] is not None]
        ch = tg[0]["channel"] if tg else ""
        if tg and len(self.cfg.talkgroups) > 1:
            ch = next((r["channel"] for r in tg if r["talkgroup"] == self.cfg.talkgroups[0]), ch)
        return {"title": self.cfg.title, "talkgroups": list(self.cfg.talkgroups), "limit": self.cfg.limit,
                "channel": ch, "rows": rows}
