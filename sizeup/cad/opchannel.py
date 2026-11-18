"""An incident's operating channel: the county talkgroup it is worked on when that isn't our dispatch talkgroup (a call
moved to another Fire/EMS talkgroup: "all units operate on Fire/EMS 31", the units switch over).

    incident_channels   incident x county talkgroup, from `since` until the incident closes or a person takes it off
                        (removed_at: never put back by a rule, only by a person). source, the first that found it:
                          dispatch  a transcript on our talkgroups or a page that is on this open incident (its page, 60
                                    Control or one of its units on our talkgroup; on MAX_INCIDENTS at most: a page and
                                    the feed's own event for the same call, or units of two calls moved together, each
                                    get it) says to operate on it:
                                    "operate on Fire/EMS 31", "switching to fire 31", "switch to 31", "go to tac 31"; read against
                                    the talkgroups' own names (Cad.source_rows: the county's Fire/EMS talkgroups)
                          feed      its external CAD feed's comments or extra "channel" name it ("FIRE/EMS 31")
                          units     UNITS_MIN of its own units (their radio IDs: the unit rule) heard on it while open
                          manual    a person ("+ channel" on the incident's radio card)
                        A dismissed incident never has one; a talkgroup of ours (Fire/EMS 15, the feed's and the status
                        hints' talkgroups) is never one.

While it is on (since .. close), a transmission on that talkgroup goes on the incident:
    unit / mention   its radio is a unit on the incident (comms.py's unit rule; or its county alias is one), it names a
                     unit of it, its address or place (radiolog.py, as on our own talkgroup)
    opchannel        the rest, unless it can be told to be another call's: the radio (or the alias) of a board unit
                     that isn't on the incident, words sending someone elsewhere or naming another open incident's unit
                     or address. Two of our incidents on one talkgroup at that time: neither. Shown folded under the
                     channel; like a mention, its audio is kept only once a person keeps it (comms.py AUTO_UNSURE).
    A late call (#747/#753) is matched by its own start, never by when it arrived.

Speech-to-text (promoted): while an incident of ours that has one is open, and TAIL_S after it closed (units still talk
there), as long as the call is heard there (QUIET_S), the talkgroup is transcribed like Fire/EMS 15 (App._stt_class URGENT: never shed; the station's FIRE input
lane; Amazon Transcribe within the monthly limit, never its reserve). The hub sends the set with the channel choices
(channels.hub_aws "promote").
"""

from __future__ import annotations

import json
import re

from sizeup import sourcekeys as sources
from sizeup.cad.comms import OPCHANNEL as WHY

SCHEMA = """
CREATE TABLE IF NOT EXISTS incident_channels (
  incident_id INTEGER NOT NULL,
  talkgroup INTEGER NOT NULL,           -- the county system's
  since REAL NOT NULL,                  -- its traffic is the incident's from then on
  source TEXT NOT NULL,                 -- dispatch | feed | units | manual
  evidence TEXT NOT NULL DEFAULT '',    -- the transmission that said it, "feed", the units' first transmission
  operator TEXT NOT NULL DEFAULT '',    -- the person who added it (manual) or took it off
  at REAL NOT NULL,
  removed_at REAL,                      -- a person took it off: off, and never put back by a rule
  PRIMARY KEY (incident_id, talkgroup)
);
CREATE INDEX IF NOT EXISTS incident_channels_tg ON incident_channels(talkgroup);
"""
LEAD_S = 60.0               # its traffic counts from this long before what said it
TAIL_S = 600.0              # promoted this long after the incident closed
QUIET_S = 1800.0            # ... and while it was put on, or traffic on it went on the incident, this recently
MAX_INCIDENTS = 3           # a move said on more of our incidents than this (by their links) isn't about one call
UNITS_MIN = 2               # this many of the incident's own units heard on a talkgroup (their radios): its channel
BACKFILL_MAX = 300
NAMES_TTL_S = 60.0
SOURCES = ("dispatch", "feed", "units", "manual")
OPEN = ("queued", "pending", "active")

# -- reading a channel assignment -----------------------------------------------------------------------------------------

TOKEN = re.compile(r"[a-z]+|\d+|[.,;:!?]")
END = "."                   # a clause's end (the punctuation a transcript has, all one token)
ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
         "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40}
# A verb that moves units to a channel, then optionally "over", "the"; a channel number alone only after BARE_VERBS
# ("switch to 31"; "go to 10" may be anything)
VERBS = ({"operate", "operating", "operations"}, {"switch", "switching", "switched", "go", "going", "move", "moving",
                                                  "change", "changing", "head", "come"})
BARE_VERBS = {"operate", "operating", "operations", "switch", "switching", "switched", "move", "moving", "change",
              "changing"}
# The Fire/EMS family as said or misheard ("Fire/EMS", "fire and EMS", "fire EMS", "Fi MS"; FULL), and the shorter words
# for the same numbered channel ("fire 31", "tac 31", "ops 31"). Never "channel N" / "talkgroup N": our own FM channels
# are called that too.
FULL = (("fire", "and", "ems"), ("fire", "e", "m", "s"), ("fire", "ems"), ("fire", "ms"), ("fi", "ems"), ("fi", "ms"),
        ("far", "ems"), ("fire", "emts"))
FAMILY = (*FULL, ("fire",), ("tac",), ("tactical",), ("ops",))
# what may follow the channel's number: the clause ends, or one of these ("switch to 31 and ...", "fire 31 for the
# command"); never a house number's street ("the fire 31 Sample Road")
AFTER = {END, "and", "now", "please", "for", "when", "on", "with", "thanks", "thank", "copy", "all", "units", "unit",
         "command", "control", "over", "time", "at"}
RUN_TOGETHER = 6            # a Fire/EMS number of this many digits or more: a page's channel and what followed, run together
FIRE_NAME = re.compile(r"^\s*fire\s*/?\s*(?:and\s+)?ems\s+(\d{1,3})\s*$", re.I)


def tokens(text: str) -> list[str]:
    return [END if t in ".,;:!?" else t for t in TOKEN.findall((text or "").lower().replace("/", " "))]


def _number(toks: list[str], i: int) -> tuple[str, int] | None:
    """(the number's digits, tokens used) at i: '31', 'thirty one', 'thirty-one', 'nineteen'."""
    if i >= len(toks):
        return None
    t = toks[i]
    if t.isdigit():
        return t, 1
    if t in TEENS:
        return str(TEENS[t]), 1
    if t in TENS:
        if i + 1 < len(toks) and toks[i + 1] in ONES:
            return str(TENS[t] + ONES[toks[i + 1]]), 2
        return str(TENS[t]), 1
    if t in ONES:
        return str(ONES[t]), 1
    return None


def _verb(toks: list[str], i: int) -> tuple[int, bool] | None:
    """(the index after a moving verb at i: "operate on", "switching over to", "go to the"; whether "the" was said),
    else None."""
    t = toks[i]
    j = i + 1
    if t in VERBS[0]:
        if j >= len(toks) or toks[j] != "on":
            return None
    elif t in VERBS[1]:
        if j < len(toks) and toks[j] == "over":
            j += 1
        if j >= len(toks) or toks[j] != "to":
            return None
    else:
        return None
    j += 1
    the = j < len(toks) and toks[j] == "the"
    return j + the, the


def _family(toks: list[str], j: int) -> tuple[tuple, int]:
    for f in FAMILY:
        if tuple(toks[j:j + len(f)]) == f:
            return f, j + len(f)
    return (), j


def parse(text: str, channels: dict[int, str], strict: bool = True) -> list[int]:
    """The talkgroups a transmission (or a feed's comment, strict=False) says to operate on, in order, from the
    county's talkgroup names (channels: {tg: name}): a Fire/EMS number after a moving verb ("operate on Fire/EMS 31",
    "switching to fire 31", "go to tac 31", "switch to 31"), a whole other name after one ("operate on Tech Rescue"); not
    strict, also a Fire/EMS name alone ("FIRE/EMS 31", "OPS ON FIRE/EMS 31"). The number must end the clause or go on
    with AFTER ("switch to 31 and ..."): never a house number. A number alone only after switch / move / change /
    operate; "the fire" is the call, never a channel. A Fire/EMS number of RUN_TOGETHER digits or more (a page's
    "Fire EMS 31540963": the channel and what followed, run together) is read as its first two digits when those are a
    channel, else its first."""
    toks = tokens(text)
    fire: dict[str, int] = {}
    named: list[tuple[list[str], int]] = []
    for tg, name in channels.items():
        m = FIRE_NAME.match(name or "")
        if m:
            fire[str(int(m.group(1)))] = tg
        else:
            nt = [w for w in tokens(name) if w != END]
            if nt and not nt[0].isdigit():
                named.append((nt, tg))
    out: list[int] = []

    def take(tg: int) -> None:
        if tg not in out:
            out.append(tg)

    for i in range(len(toks)):
        v = _verb(toks, i)
        if v is None:
            if strict:
                continue                                 # said on the air: a moving verb, always
            j, the = i, False
        else:
            j, the = v
        fam, k = _family(toks, j)
        if v is None and fam not in FULL:
            continue                                     # a feed's comment: the Fire/EMS name in full
        if the and fam == ("fire",):
            continue                                     # "go to the fire 31 ...": the call, not a channel
        got = _number(toks, k)
        if got is not None:
            digits, used = got
            after = toks[k + used] if k + used < len(toks) else END
            if len(digits) <= 3 and str(int(digits)) in fire:
                if (fam or toks[i] in BARE_VERBS) and after in AFTER:
                    take(fire[str(int(digits))])
                    continue
            elif fam in FULL and len(digits) >= RUN_TOGETHER:
                first = next((d for d in (str(int(digits[:2])), str(int(digits[:1]))) if d in fire), "")
                if first:
                    take(fire[first])
                    continue
        if v is not None:
            for nt, tg in named:
                if toks[j:j + len(nt)] == nt and (toks[j + len(nt)] if j + len(nt) < len(toks) else END) in AFTER:
                    take(tg)
    return out


def _key(s: str) -> str:
    return re.sub(r"[\s-]+", "", str(s or "")).upper()


class OpChannels:
    def __init__(self, cad):
        self.cad, self.store = cad, cad.store
        self.on_change = None           # () -> None: the promoted set may have changed (the hub tells its station)
        self._names: tuple[float, dict[int, str]] = (-1e18, {})
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    def _log(self, m: str) -> None:
        try:
            self.cad.log(m)
        except Exception:  # noqa: BLE001
            pass

    def _changed(self) -> None:
        fn = self.on_change
        if fn is not None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - the hub's push is tried again at the next state post anyway
                self._log(f"CAD: operating channel push: {type(e).__name__}")

    # -- which talkgroups may be one ---------------------------------------------------------------------------------------

    def names(self) -> dict[int, str]:
        """The county's fire talkgroups that may be an operating channel: {tg: name}, from the talkgroups the monitor
        lists (Cad.source_rows; the hub: its station's), group Fire/EMS, not encrypted, never one of ours (ours())."""
        now = self.cad.clock()
        if 0 <= now - self._names[0] < NAMES_TTL_S:
            return self._names[1]
        out: dict[int, str] = {}
        try:
            rows = self.cad.source_rows() or []
        except Exception:  # noqa: BLE001
            rows = []
        ours = self.ours()
        for r in rows:
            if not isinstance(r, dict) or r.get("encrypted") or (r.get("system") or sources.COUNTY) != sources.COUNTY:
                continue
            tg, name = r.get("tg"), str(r.get("name") or "")
            if not isinstance(tg, int) or isinstance(tg, bool) or tg in ours:
                continue
            if "fire" not in str(r.get("group") or "").lower() or re.search(r"control|dispatch", name, re.I):
                continue
            out[tg] = name or f"TG {tg}"
        self._names = (now, out)
        return out

    def ours(self) -> set[int]:
        """Our own talkgroups (radiolog: the incident talkgroups, the feed's, the status hints'): never one."""
        try:
            return set(self.cad.radiolog._channels()[0])
        except Exception:  # noqa: BLE001
            return set(self.cad.s.incident_talkgroups)

    def name(self, tg: int) -> str:
        return self.names().get(int(tg)) or f"TG {int(tg)}"

    # -- reading ----------------------------------------------------------------------------------------------------------

    def of(self, ids: list[int]) -> dict[int, list[dict]]:
        """Each incident's channels on now: [{tg, name, since, source, operator}], oldest first."""
        out: dict[int, list[dict]] = {i: [] for i in ids}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            for r in self.store.q(f"SELECT * FROM incident_channels WHERE removed_at IS NULL AND incident_id IN "
                                  f"({','.join('?' * len(part))}) ORDER BY since, talkgroup", part):
                out[r["incident_id"]].append({"tg": r["talkgroup"], "name": self.name(r["talkgroup"]), "since": r["since"],
                                              "source": r["source"], "operator": r["operator"]})
        return out

    def windows(self, lo: float, hi: float, tg: int | None = None) -> list[dict]:
        """The channels on at some time in [lo, hi]: [{incident_id, tg, since, until (None: still open)}]; never a
        dismissed incident's, never one a person took off."""
        rows = self.store.q("SELECT c.incident_id, c.talkgroup AS tg, c.since, i.closed_at AS until FROM incident_channels c "
                            "JOIN incidents i ON i.id = c.incident_id WHERE c.removed_at IS NULL AND i.status != 'dismissed' "
                            "AND c.since <= ? AND (i.closed_at IS NULL OR i.closed_at >= ?)"
                            + (" AND c.talkgroup = ?" if tg is not None else ""), (hi, lo, *([int(tg)] if tg is not None else [])))
        return [dict(r) for r in rows]

    @staticmethod
    def on_at(wins: list[dict], tg, at: float) -> list[int]:
        """The incidents whose channel tg is at that time."""
        return sorted({w["incident_id"] for w in wins if w["tg"] == tg and w["since"] <= at
                       and (w["until"] is None or w["until"] >= at)})

    def promoted(self, now: float | None = None) -> set[int]:
        """The talkgroups transcribed like ours now: an open incident's channel, or one closed in the last TAIL_S; and
        only while the call is heard there (put on in the last QUIET_S, or a transmission on it on the incident in the
        last QUIET_S): a call left open for hours never keeps a busy shared talkgroup ours."""
        now = self.cad.clock() if now is None else now
        rows = self.store.q("SELECT DISTINCT c.talkgroup FROM incident_channels c JOIN incidents i ON i.id = c.incident_id "
                            f"WHERE c.removed_at IS NULL AND (i.status IN ({','.join('?' * len(OPEN))}) OR "
                            "(i.status = 'closed' AND i.closed_at >= ?)) AND (c.since >= ? OR c.at >= ? OR EXISTS ("
                            "SELECT 1 FROM radio_links l JOIN radio_traffic t ON t.id = l.traffic_id WHERE "
                            "l.incident_id = c.incident_id AND t.talkgroup = c.talkgroup AND t.start >= ?))",
                            (*OPEN, now - TAIL_S, now - QUIET_S, now - QUIET_S, now - QUIET_S))
        return {r["talkgroup"] for r in rows} - self.ours()

    def promoted_since(self) -> dict[str, float]:
        """{"tg:N": since} for the channels on now (not removed, not dismissed): when the move to each was (its earliest),
        for the station to raise only what was heard after it."""
        rows = self.store.q("SELECT c.talkgroup, MIN(c.since) AS since FROM incident_channels c JOIN incidents i ON "
                            "i.id = c.incident_id WHERE c.removed_at IS NULL AND i.status != 'dismissed' GROUP BY c.talkgroup")
        return {f"tg:{r['talkgroup']}": float(r["since"]) for r in rows}

    # -- writing ----------------------------------------------------------------------------------------------------------

    def assign(self, iid: int, tg: int, source: str, since: float, evidence: str = "", operator: str = "") -> bool:
        """The incident is worked on this talkgroup from `since` (the earlier when it already was). A person's removal
        stands against every rule (only a person adds it back). True: on now that wasn't, or from earlier."""
        tg = int(tg)
        inc = self.store.one("SELECT id, status FROM incidents WHERE id = ?", (iid,))
        if inc is None or inc["status"] == "dismissed" or tg in self.ours() or source not in SOURCES:
            return False
        now = self.cad.clock()
        with self.store.tx() as db:
            had = db.execute("SELECT * FROM incident_channels WHERE incident_id = ? AND talkgroup = ?", (iid, tg)).fetchone()
            if had is not None and had["removed_at"] is not None and source != "manual":
                return False
            if had is not None and had["removed_at"] is None:
                if since >= had["since"]:
                    return False
                db.execute("UPDATE incident_channels SET since = ? WHERE incident_id = ? AND talkgroup = ?", (since, iid, tg))
            else:
                db.execute("INSERT OR REPLACE INTO incident_channels (incident_id, talkgroup, since, source, evidence, operator, "
                           "at, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)", (iid, tg, since, source, evidence[:200],
                                                                                   operator[:120], now))
                self.store._event(db, iid, "radio", f"Channel {self.name(tg)} ({source})", operator)
        self._log(f"CAD: event {iid} operating channel TG {tg} ({source})")
        self.backfill_soon(iid, tg)
        self._changed()
        return True

    def remove(self, iid: int, tg: int, operator: str = "") -> bool:
        """A person takes it off: its "on this channel" links that nobody kept go too; no rule puts it back."""
        tg = int(tg)
        with self.store.tx() as db:
            n = db.execute("UPDATE incident_channels SET removed_at = ?, operator = ? WHERE incident_id = ? AND talkgroup = ? "
                           "AND removed_at IS NULL", (self.cad.clock(), operator[:120], iid, tg)).rowcount
            if not n:
                return False
            gone = [r[0] for r in db.execute(
                "SELECT l.traffic_id FROM radio_links l JOIN radio_traffic t ON t.id = l.traffic_id WHERE l.incident_id = ? "
                "AND l.why = ? AND t.talkgroup = ? AND NOT EXISTS (SELECT 1 FROM radio_link_kept k WHERE "
                "k.traffic_id = l.traffic_id AND k.incident_id = l.incident_id)", (iid, WHY, tg))]
            for tid in gone:
                db.execute("DELETE FROM radio_links WHERE traffic_id = ? AND incident_id = ? AND why = ?", (tid, iid, WHY))
                self.cad.comms.touch(tid)
            self.store._event(db, iid, "radio", f"Channel {self.name(tg)} removed", operator)
        self.cad.comms.rev += 1
        self._changed()
        return True

    # -- the links ------------------------------------------------------------------------------------------------------

    def rule_links(self, t: dict, rigs: list[str], ctx: dict | None = None) -> tuple[dict[int, tuple[str, str]], set[int]]:
        """For Comms.record (before its transaction; t as it takes it): ({incident: (why, unit)} to link, {incident}
        whose unkept "on this channel" link this transmission must not have). Nothing off an operating channel. ctx: the
        radio log's context, when the caller has one for this time (a backfill)."""
        tg = t.get("talkgroup")
        if tg is None or (t.get("system") or sources.COUNTY) != sources.COUNTY:
            return {}, set()
        start = float(t["start"])
        wins = self.windows(start, start, int(tg))
        ids = self.on_at(wins, int(tg), start)
        if not ids:
            return {}, set()
        if len(ids) > 1:
            return {}, set(ids)                          # two of ours on one talkgroup: can't tell whose
        iid = ids[0]
        rl = self.cad.radiolog
        ctx = ctx or rl.context(start, start)
        on = rl._units_on(ctx, iid, start)
        if rigs:                                         # a radio we know: its unit's call (as the unit rule), or not this one
            mine = sorted(set(rigs) & on)
            return ({iid: ("unit", mine[0])}, set()) if mine else ({}, {iid})
        add: dict[int, tuple[str, str]] = {}
        from sizeup.cad.radios import is_dispatcher
        for name in [x.strip() for x in (t.get("unit") or "").split(",") if x.strip()]:
            if is_dispatcher(name):
                continue
            b = ctx["board"].get(_key(name)) or ctx["board"].get(_key(name.split()[0]))
            if b is None or self.store.lists.placeholder(b[0]):
                continue
            if b[0] not in on:
                return {}, {iid}                         # a board unit that isn't on it: another call's
            add[iid] = ("unit", b[0])
        text = (t.get("text") or "").strip()
        if text:
            from sizeup.cad.radiolog import sent_elsewhere
            inc = next((i for i in ctx["incs"] if i["id"] == iid), None)
            if inc is not None and sent_elsewhere(text, inc):
                return {}, {iid}
            row = {"talkgroup": int(tg), "channel": t.get("source") or "", "system": sources.COUNTY, "start": start,
                   "text": text, "rig": ""}
            found = rl.matches(row, ctx)
            if found and iid not in found:
                return {}, {iid}                         # it names another incident's unit, address or place
        add.setdefault(iid, (WHY, self.name(int(tg))))
        return add, set()

    def backfill(self, iid: int, tg: int) -> int:
        """A channel just put on (or from earlier): its transmissions since then matched (newest first, BACKFILL_MAX),
        by the rules above and the radio log's. How many went on."""
        wins = [w for w in self.windows(-1e18, 1e18, tg) if w["incident_id"] == iid]
        if not wins:
            return 0
        lo, hi = wins[0]["since"], min(wins[0]["until"] or 1e18, self.cad.clock() + 1)
        rows = self.store.q("SELECT * FROM radio_traffic WHERE talkgroup = ? AND system IN ('', ?) AND start >= ? AND start <= ? "
                            "ORDER BY start DESC LIMIT ?", (int(tg), sources.COUNTY, lo, hi, BACKFILL_MAX))
        if not rows:
            return 0
        ctx = self.cad.radiolog.context(min(r["start"] for r in rows), max(r["start"] for r in rows))
        n = 0
        for r in rows:
            t = {"id": r["id"], "start": r["start"], "talkgroup": r["talkgroup"], "system": r["system"], "unit": r["talker"],
                 "text": r["text"], "source": r["channel"]}
            add, drop = self.rule_links(t, [x.strip() for x in (r["rig"] or "").split(",") if x.strip()], ctx)
            with self.store.tx() as db:
                before = db.total_changes
                self.cad.comms._channel_apply(db, r["id"], add, drop)
                n += int(db.total_changes != before and iid in add)
            if (r["text"] or "").strip():
                f = self.cad.radiolog.matches(r, ctx)
                if iid in f:
                    self.cad.radiolog._link(r["id"], {iid: f[iid]})
        if n:
            self.cad.comms.rev += 1
        return n

    def backfill_soon(self, iid: int, tg: int) -> None:
        """The backfill off the caller's thread (the radio log's workers: stopped and joined with them); inline for
        tests."""
        rl = self.cad.radiolog

        def run():
            try:
                self.backfill(iid, tg)
            except Exception as e:  # noqa: BLE001
                self._log(f"CAD: operating channel backfill: {type(e).__name__}")
        if rl.inline:
            run()
            return
        with rl._qlock:
            if not rl._closing:
                rl._spawn_locked(run, "opchannel-backfill")

    # -- finding one ------------------------------------------------------------------------------------------------------

    def heard(self, tid: str) -> list[int]:
        """A transcript on one of our talkgroups or paging channels (never a fireground: its own "tac 2") that is on open incidents of
        ours (by any rule but this one's; MAX_INCIDENTS at most) and says to operate on one talkgroup: their channel
        from LEAD_S before it was said. The incidents that got one."""
        t = self.store.one("SELECT id, start, channel, talkgroup, system, text FROM radio_traffic WHERE id = ?", (tid,))
        if t is None or not (t["text"] or "").strip() or (
                t["talkgroup"] is not None and (t["system"] or sources.COUNTY) != sources.COUNTY):
            return []
        names = self.names()
        said = parse(t["text"], names) if names else []
        if len(said) != 1:
            return []                                    # none, or more than one: not a clear move
        tgs, _fm = self.cad.radiolog._channels()
        if (t["talkgroup"] not in tgs) if t["talkgroup"] is not None else (t["channel"] not in set(self.cad.s.paging)):
            return []                                    # said on our talkgroups or a page (a fireground's "tac 2" is ours)
        incs = self.store.q("SELECT DISTINCT i.id, i.status, i.closed_at FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                            "WHERE l.traffic_id = ? AND l.why != ? AND i.status != 'dismissed'", (tid, WHY))
        incs = [i for i in incs if i["closed_at"] is None or i["closed_at"] >= t["start"]]
        if not incs or len(incs) > MAX_INCIDENTS:
            return []                                    # on none of ours, or about more than one call
        return [i["id"] for i in incs if self.assign(i["id"], said[0], "dispatch", float(t["start"]) - LEAD_S, evidence=tid)]

    def after_record(self, t: dict, iids: list[int]) -> None:
        """Comms.record just logged it (linked to iids): our units' radios heard on another talkgroup while on one of
        ours (UNITS_MIN units: that talkgroup is its channel). A transcript that moves the call: on_text (heard)."""
        tid = str(t["id"])
        tg = t.get("talkgroup")
        if tg is None or not iids or (t.get("system") or sources.COUNTY) != sources.COUNTY or int(tg) in self.ours() \
                or int(tg) not in self.names():
            return
        for iid in iids:
            r = self.store.one("SELECT COUNT(DISTINCT l.unit) AS n, MIN(t.start) AS first FROM radio_links l JOIN radio_traffic t "
                               "ON t.id = l.traffic_id JOIN incidents i ON i.id = l.incident_id WHERE l.incident_id = ? AND "
                               f"l.why = 'unit' AND t.talkgroup = ? AND t.system IN ('', ?) AND i.status IN ({','.join('?' * len(OPEN))})",
                               (iid, int(tg), sources.COUNTY, *OPEN))
            if r and r["n"] >= UNITS_MIN:
                have = self.store.one("SELECT 1 FROM incident_channels WHERE incident_id = ? AND talkgroup = ?", (iid, int(tg)))
                if have is None:
                    self.assign(iid, int(tg), "units", float(r["first"]) - LEAD_S, evidence=tid)

    def scan_feed(self, iid: int | None) -> list[int]:
        """The incident's external CAD feed (its comments, extra "channel") names one talkgroup to work on: its channel
        from the call's dispatch (or the comment's time) less LEAD_S."""
        if not iid:
            return []
        names = self.names()
        if not names:
            return []
        from sizeup.cad.sources import PAGING
        found: dict[int, float] = {}
        for r in self.store.q("SELECT data FROM source_reports WHERE incident_id = ? AND source != ? AND merged_into = ''",
                              (iid, PAGING)):
            d = json.loads(r["data"] or "{}")
            texts: list[tuple[float | None, str]] = []
            ch = (d.get("extra") or {}).get("channel")
            if isinstance(ch, str):
                texts.append((None, ch))
            for c in d.get("comments") or []:
                if isinstance(c, (list, tuple)) and len(c) == 2 and isinstance(c[1], str):
                    texts.append((c[0] if isinstance(c[0], (int, float)) else None, c[1]))
            for at, text in texts:
                for tg in parse(text, names, strict=False):
                    t0 = at if at is not None else self.cad.radiolog.dispatched_at(iid)
                    found[tg] = min(found.get(tg, t0), t0)
        if len(found) != 1:
            return []
        [(tg, t0)] = found.items()
        return [iid] if self.assign(iid, tg, "feed", t0 - LEAD_S, evidence="feed") else []
