"""Communications log: every transmission the monitor transcribed, tied to a rig and to CAD events.

    radio_traffic   one row per transmission (the monitor's clip id: its audio plays from
                    /api/audio/<id>.wav until the 12 h audio limit; the transcript is copied here
                    and kept), with the rig(s) the registry resolved and the raw radio ID(s); an FG1
                    key-up with no ID says why (id_note, #163): none (no ID burst: the radio sent
                    none), undecoded (a burst that didn't decode), weak (too weak to tell), '' (not known).
                    emergency: the radio's emergency button was on it (the P25 call JSON's flag, an MDC-1200 /
                    FleetSync emergency packet: #128), kept on the row so the log shows it whatever became of
                    its alarm; evidence: for an emergency P25 call, trunk-recorder's own call record (its JSON:
                    srcList, freqList, the flags), kept for audit after the recorder's files are gone.
                    Read back, each row also carries its alarm when it raised one (with_alarms: alarm_id,
                    alarm_level alarm | notice, keywords, alarm_via, ack_by, ack_at, drill), so rows
                    logged before the flag was kept light up too.
    radio_links     transmission x event, and why:
                      unit        from a radio of a unit assigned to the event (dispatched or
                                  attached, until it cleared) at the time of the transmission: the
                                  radio ID is in the registry and names that unit. The only way a
                                  transmission on FG1 gets on an event by itself (#163: "these don't
                                  have radio ids and were added to this inc ... we should only add them
                                  if we know it, or ask if we see a new id"). A transmission with no ID
                                  stays in the Radio view; an ID the registry doesn't know is asked
                                  about (sizeup/cad/newids.py), and once it is named its transmissions
                                  are linked like any other (relink(): the ones already logged too).
                                  (Until #163, an unidentified FG1 radio was put on the one emergency
                                  event with units, why "channel"; those links are dropped on start.)
                      dispatcher  a 60 Control turn right before or after a linked unit's turn in the
                                  same P25 call on an incident talkgroup (Fire/EMS 15); a key-up of
                                  an unknown radio between them (no ID, issue #62) is looked past
                      airport     a P25 transmission on the Airport Incident talkgroup (333) while
                                  exactly one Airport event (type AIRPORT, #87) is open: the whole
                                  talkgroup is that incident's, whoever is talking, as long as the
                                  talker is identified (a radio ID or alias: a key-up with neither is
                                  not linked, #163). Two open: can't tell whose, no link; none open:
                                  nothing.
                      manual      put on the event by a person (a transmission's menu, "Add to event");
                                  unit = the operator who did it (also in the event's timeline)
                      page        a 60 Control page (South 2) on the event it opened, or a repeat of it
                                  (Cad.from_monitor): the event's page plays from the event itself
    A page is a row like any transmission (id = the page id, channel South 2, talker 60 Control), with
    its tone pairs (tones: [{a_hz, b_hz, names, ours}], JSON) and where the voice starts (voice_at, seconds
    into the clip: play skips the tones). Read back, each row says what kind of page it is (page, #80):
    "ours" (a pair names PCFD / Rye Brook: the event it opened is on it), "other" (another department's),
    "unknown" (a pair nobody's code matches: "Name these tones"), "none" (a paging channel's transmission
    with no tones decoded: voice only, or a missed decode), "" (not a page). tone_seq: every tone heard in
    order, {hz, start, dur, role: A | B | AB | long | ""} (#115: "" = in no pair, a broken one), read back as a
    list; names_ours: a page that isn't ours whose text names PCFD's or Rye Brook's units (#115's safety net,
    Comms.names_ours set by the app from the feed's unit matcher): [unit codes], else [].
    radio_link_exclusions  transmission x event a person took off ("Remove from 26-000007", user,
                    2026-10-08: "how do i remove things from an inc if it was added"): an auto link
                    (unit / dispatcher / airport) removed is remembered here so the auto-linker never
                    puts it back (the transcript coming again, the other turn of the call arriving);
                    a manual add clears it. A manual link removed is just gone.

Links are made when the transcript arrives and never moved afterwards (but by a person); a re-run of
speech-to-text updates the copied text. Adding and removing each write a line in the event's chronology.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

from sizeup.cad import airport
from sizeup.cad.radios import is_dispatcher
from sizeup.cad.store import TZ

TURN = re.compile(r"^(?P<call>.+)-t(?P<k>\d+)$")
NOTES = "Notes"          # the /comms "channel" of event notes tagged with a unit
KEEP_UNLINKED_S = 90 * 24 * 3600     # transmissions on no event: kept 90 days (linked ones: always)
MAX_ROWS = 500
ID_NOTES = ("none", "undecoded", "weak")    # why an FG1 key-up has no radio ID (radio_traffic.id_note, #163)
RELINK_MAX = 2000                           # a radio just named: at most this many of its unnamed transmissions
EVIDENCE_MAX = 64 * 1024                    # an emergency's raw call record (radio_traffic.evidence), at most
# A transmission that had the emergency button or raised an alarm (the Radio view's Emergency filter)
EMERGENCY_WHERE = "(t.emergency = 1 OR t.id IN (SELECT traffic_id FROM alarms))"


def split_call(tid: str, p25: bool) -> tuple[str, int | None]:
    """'307-1790954731.970_476112500.0-call_11-t2' -> (call id, 2); a one-talker call -> (its id, 0)."""
    if not p25:
        return "", None
    m = TURN.match(tid)
    return (m.group("call"), int(m.group("k"))) if m else (tid, 0)


class Comms:
    def __init__(self, store, tactical: tuple[str, ...] = ("FG1",), incident_tgs: tuple[int, ...] = (307, 333),
                 clock=time.time, paging: tuple[str, ...] = ("South 2",), airport_tgs: tuple[int, ...] = (333,)):
        self.store, self.clock = store, clock
        self.tactical, self.incident_tgs, self.paging = set(tactical), set(incident_tgs), set(paging)
        self.airport_tgs = set(airport_tgs)      # the Airport Incident talkgroup(s): an open Airport event's (#87)
        self.rev = 0            # bumped on every new or changed transmission (the /comms page polls it)
        self.names_ours = None  # text -> [our unit codes] (the feed's matcher, #115): a page not ours naming them
        self.on_text = None     # traffic id -> None: a transcript arrived or changed (status suggestions, #37)
        self._drop_channel_links()

    def _drop_channel_links(self) -> None:
        """The links the old FG1 channel rule made (an unidentified radio put on the one call with units): gone
        (#163). A person's own (manual) links stay."""
        try:
            with self.store.tx() as db:
                db.execute("DELETE FROM radio_links WHERE why = 'channel'")
        except Exception:
            pass                # no comms tables yet (a fresh store migrates first)

    # -- writing --------------------------------------------------------------------------------

    def record(self, t: dict, replace: bool = True) -> list[int]:
        """One transmission: {id, start, end, source, talkgroup, unit (talker as heard), rig, radio_id,
        text, status, model, emergency?, evidence?}. Returns the event ids it is linked to. replace=False: skip
        one already logged (the startup backfill). An emergency once logged stays (a later write without the
        flag never clears it), and so does its evidence."""
        tid = str(t["id"])
        p25 = t.get("talkgroup") is not None
        call_id, turn = split_call(tid, p25)
        talker = t.get("unit") or ""
        disp = int(p25 and bool(talker) and all(is_dispatcher(x) for x in talker.split(",")))
        now = self.clock()
        start = float(t["start"])
        rigs = [r.strip() for r in (t.get("rig") or "").split(",") if r.strip()]
        with self.store.tx() as db:
            have = db.execute("SELECT 1 FROM radio_traffic WHERE id = ?", (tid,)).fetchone()
            if have and not replace:
                return []
            tones = json.dumps(t["tones"]) if t.get("tones") else ""
            seq = json.dumps(t["tone_seq"]) if t.get("tone_seq") else ""
            known = bool(rigs) or bool(t.get("radio_id")) or bool(talker)
            note = "" if known else str(t.get("id_note") or "")
            note = note if note in ID_NOTES else ""
            ev = evidence_json(t.get("evidence"))
            vals = (call_id, turn, start, float(t.get("end") or start), t.get("source") or "", t.get("talkgroup"),
                    talker, t.get("radio_id") or "", ", ".join(rigs), disp, t.get("text") or "", t.get("status") or "",
                    t.get("model") or "", tones, t.get("voice_at"), seq, note, int(bool(t.get("emergency"))),
                    str(t.get("kept") or "")[:40], ev, now)
            if have:
                db.execute("UPDATE radio_traffic SET call_id = ?, turn = ?, start = ?, end = ?, channel = ?, talkgroup = ?, "
                           "talker = ?, radio_id = ?, rig = ?, dispatcher = ?, text = ?, status = ?, model = ?, tones = ?, "
                           "voice_at = ?, tone_seq = ?, id_note = ?, emergency = MAX(emergency, ?), "
                           "kept = COALESCE(NULLIF(?, ''), kept), evidence = COALESCE(NULLIF(?, ''), evidence), "
                           "updated_at = ? WHERE id = ?", (*vals, tid))
            else:
                db.execute("INSERT INTO radio_traffic (call_id, turn, start, end, channel, talkgroup, talker, radio_id, rig, "
                           "dispatcher, text, status, model, tones, voice_at, tone_seq, id_note, emergency, kept, evidence, "
                           "updated_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*vals, tid))
            off = {r[0] for r in db.execute("SELECT incident_id FROM radio_link_exclusions WHERE traffic_id = ?", (tid,))}
            links: dict[int, tuple[str, str]] = {}
            for rig in rigs:
                for r in db.execute(self.ON_EVENT_BY_UNIT, (rig, start, start)).fetchall():
                    links.setdefault(r["incident_id"], ("unit", rig))
            # No ID, no link (#163): an unidentified FG1 radio stays in the Radio view, whoever's call is running.
            if p25 and known and t.get("talkgroup") in self.airport_tgs:
                open_ap = db.execute(self.OPEN_AIRPORT, (airport.TYPE, start, start)).fetchall()
                if len(open_ap) == 1:           # the Airport Incident talkgroup: the one open airport event's
                    links.setdefault(open_ap[0]["id"], ("airport", ""))
            for iid, (why, unit) in links.items():
                if iid not in off:              # a person took it off that event: not again
                    self._auto(db, tid, iid, why, unit)
            if p25 and turn is not None and t.get("talkgroup") in self.incident_tgs:
                self._dispatcher_links(db, tid, call_id, turn, bool(disp), links)
            self.rev += 1
            out = sorted(r[0] for r in db.execute("SELECT incident_id FROM radio_links WHERE traffic_id = ?", (tid,)))
        self._heard(tid)
        return out

    def _heard(self, tid: str) -> None:
        if self.on_text is not None:
            try:
                self.on_text(tid)
            except Exception as e:  # noqa: BLE001 - a suggestion never stops the log
                print(f"CAD: status suggestion for {tid}: {type(e).__name__}: {e}")

    # A unit's assignment covering time T: from when it was put on the event until it cleared.
    ON_EVENT_BY_UNIT = ("SELECT DISTINCT a.incident_id FROM assignments a JOIN units u ON u.id = a.unit_id "
                        "WHERE u.name = ? AND a.assigned_at IS NOT NULL AND a.assigned_at <= ? "
                        "AND (a.cleared_at IS NULL OR a.cleared_at >= ?)")
    # Airport events (#87) open at time T: created by then and not yet closed or dismissed (a late transcript
    # from while it was open still links).
    OPEN_AIRPORT = "SELECT id FROM incidents WHERE type = ? AND created_at <= ? AND (closed_at IS NULL OR closed_at >= ?)"
    # Emergency events with any unit on them at time T (their tactical channel is in use): the alarms' FG1 mayday
    # with no ID (sizeup/cad/alarms.py) and the new-ID prompt's "during 26-000016" (sizeup/cad/newids.py).
    ACTIVE_EVENTS = ("SELECT DISTINCT a.incident_id FROM assignments a JOIN incidents i ON i.id = a.incident_id "
                     "WHERE i.emergency = 1 AND a.assigned_at IS NOT NULL AND a.assigned_at <= ? "
                     "AND (a.cleared_at IS NULL OR a.cleared_at >= ?)")

    def _dispatcher_links(self, db, tid: str, call_id: str, turn: int, disp: bool, mine: dict) -> None:
        """60 Control answering (or calling) a linked unit: the turn right before or after it in the
        same call. Either side may be transcribed first, so both directions are checked. A turn of an
        unknown radio (no talker, no ID) in between is looked past; one not logged yet stops the look
        (it may be the unit that was really answered), and when it comes in unknown it bridges the two."""
        rows = {r["turn"]: r for r in db.execute(
            "SELECT id, turn, dispatcher, talker, radio_id, rig FROM radio_traffic WHERE call_id = ?", (call_id,))}

        def near(step: int):
            k = turn + step
            while k in rows:
                if rows[k]["talker"] or rows[k]["radio_id"] or rows[k]["rig"]:
                    return rows[k]
                k += step
            return None

        sib = [s for s in (near(-1), near(1)) if s is not None]
        me = rows.get(turn)
        if me is not None and not (me["talker"] or me["radio_id"] or me["rig"]):
            # me: an unknown radio's key-up; the turns on either side may be 60 Control and a unit
            if len(sib) == 2 and sib[0]["dispatcher"] != sib[1]["dispatcher"]:
                d, u = sib if sib[0]["dispatcher"] else sib[::-1]
                for r in db.execute("SELECT incident_id, unit FROM radio_links WHERE traffic_id = ? AND why = 'unit'",
                                    (u["id"],)).fetchall():
                    self._auto(db, d["id"], r["incident_id"], "dispatcher", r["unit"])
            return
        for s in sib:
            if disp and not s["dispatcher"]:            # me: dispatcher; neighbour: a unit linked by unit
                for r in db.execute("SELECT incident_id, unit FROM radio_links WHERE traffic_id = ? AND why = 'unit'",
                                    (s["id"],)).fetchall():
                    self._auto(db, tid, r["incident_id"], "dispatcher", r["unit"])
            elif not disp and s["dispatcher"]:          # me: a unit; neighbour: the dispatcher
                for iid, (why, unit) in mine.items():
                    if why == "unit":
                        self._auto(db, s["id"], iid, "dispatcher", unit)

    @staticmethod
    def _auto(db, tid: str, iid: int, why: str, unit: str) -> None:
        """An auto link, unless a person took this transmission off that event (radio_link_exclusions)."""
        db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why, unit) SELECT ?, ?, ?, ? "
                   "WHERE NOT EXISTS (SELECT 1 FROM radio_link_exclusions WHERE traffic_id = ? AND incident_id = ?)",
                   (tid, iid, why, unit, tid, iid))

    def relink(self, system: str, radio_id: str, unit: str) -> list[int]:
        """A radio ID just named (the new-ID prompt, sizeup/cad/newids.py, or Settings > Radios): each of its
        transmissions logged with no rig (newest RELINK_MAX) gets that rig, and goes on the events the unit was on
        when it was made, as record() would have put it (#163: "including that incident's recent ones"), unless a
        person took it off one. A row that already had a rig keeps it (named as it was then). Returns the event
        ids that got a transmission."""
        rid = str(radio_id or "").strip()
        rid = rid.upper() if system == "mdc" else rid
        unit = str(unit or "").strip()
        if not rid or not unit or not re.fullmatch(r"[0-9A-Za-z-]{1,12}", rid):
            return []
        fm = system in ("mdc", "fleetsync")
        got: set[int] = set()
        with self.store.tx() as db:
            rows = db.execute("SELECT id, call_id, turn, start, talkgroup, dispatcher FROM radio_traffic WHERE rig = '' AND "
                              + ("talkgroup IS NULL" if fm else "talkgroup IS NOT NULL")
                              + " AND (', ' || radio_id || ', ') LIKE ? ORDER BY start DESC LIMIT ?",
                              (f"%, {rid}, %", RELINK_MAX)).fetchall()
            for r in rows:
                db.execute("UPDATE radio_traffic SET rig = ?, id_note = '', updated_at = ? WHERE id = ?",
                           (unit, self.clock(), r["id"]))
                mine: dict[int, tuple[str, str]] = {}
                for e in db.execute(self.ON_EVENT_BY_UNIT, (unit, r["start"], r["start"])).fetchall():
                    mine[e["incident_id"]] = ("unit", unit)
                    before = db.total_changes
                    self._auto(db, r["id"], e["incident_id"], "unit", unit)
                    if db.total_changes != before:
                        got.add(e["incident_id"])
                if not fm and r["turn"] is not None and r["talkgroup"] in self.incident_tgs:
                    self._dispatcher_links(db, r["id"], r["call_id"], r["turn"], bool(r["dispatcher"]), mine)
            if rows:
                self.rev += 1
        return sorted(got)

    def id_stats(self, since: float, channels=None) -> list[dict]:
        """Per analog channel since then (Settings > Radios, #163): transmissions, how many had a radio ID, and of
        the rest how many sent none / had a burst that didn't decode / were too weak to tell / are from before."""
        chans = list(channels if channels is not None else self.tactical)
        if not chans:
            return []
        rows = self.store.q(f"SELECT channel, COUNT(*) AS n, SUM(radio_id != '' OR talker != '') AS with_id, "
                            f"SUM(radio_id = '' AND talker = '' AND id_note = 'none') AS none, "
                            f"SUM(radio_id = '' AND talker = '' AND id_note = 'undecoded') AS undecoded, "
                            f"SUM(radio_id = '' AND talker = '' AND id_note = 'weak') AS weak "
                            f"FROM radio_traffic WHERE talkgroup IS NULL AND start >= ? AND channel IN "
                            f"({','.join('?' * len(chans))}) GROUP BY channel ORDER BY channel", (since, *chans))
        out = []
        for r in rows:
            d = {k: int(r[k] or 0) for k in ("n", "with_id", "none", "undecoded", "weak")}
            d["unknown"] = d["n"] - d["with_id"] - d["none"] - d["undecoded"] - d["weak"]
            out.append({"channel": r["channel"], **d})
        return out

    def link_pages(self, iid: int, page_ids) -> int:
        """The 60 Control pages that opened (or repeated) an event, on it (why 'page'): each one already
        in the log, unless a person took it off. Returns how many are new on it."""
        n = 0
        with self.store.tx() as db:
            for pid in dict.fromkeys(str(p) for p in page_ids if p):
                if db.execute("SELECT 1 FROM radio_traffic WHERE id = ?", (pid,)).fetchone() is None:
                    continue
                n += db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why, unit) SELECT ?, ?, 'page', '' "
                                "WHERE NOT EXISTS (SELECT 1 FROM radio_link_exclusions WHERE traffic_id = ? AND incident_id = ?)",
                                (pid, iid, pid, iid)).rowcount
            if n:
                self.rev += 1
        return n

    def link(self, tid: str, iid: int, operator: str = "") -> dict:
        """A person puts a transmission on an event (why 'manual', unit = the operator), with a line in
        the event's timeline; one they took off before (an exclusion) may come back this way. One
        already on that event is left as it was."""
        with self.store.tx() as db:
            t = db.execute("SELECT * FROM radio_traffic WHERE id = ?", (tid,)).fetchone()
            if t is None:
                return {"linked": False, "error": "no such transmission"}
            old = db.execute("SELECT why FROM radio_links WHERE traffic_id = ? AND incident_id = ?", (tid, iid)).fetchone()
            if old is not None:
                return {"linked": False, "why": old["why"]}
            db.execute("DELETE FROM radio_link_exclusions WHERE traffic_id = ? AND incident_id = ?", (tid, iid))
            db.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES (?, ?, 'manual', ?)",
                       (tid, iid, operator))
            said = (t["text"] or "").strip()
            self.store._event(db, iid, "radio", f"Radio traffic added: {_when(t)} {_who(t)} on {t['channel']}"
                              + (f": \u201c{said[:160]}{'…' if len(said) > 160 else ''}\u201d" if said else ""), operator)
            self.rev += 1
        return {"linked": True, "why": "manual"}

    def unlink(self, tid: str, iid: int, operator: str = "") -> dict:
        """A person takes a transmission off an event: a manual link is deleted; an auto one (unit,
        channel, dispatcher) too, and remembered (radio_link_exclusions) so the auto-linker never puts it
        back. A line in the event's chronology either way. One not on that event: nothing changes."""
        with self.store.tx() as db:
            t = db.execute("SELECT * FROM radio_traffic WHERE id = ?", (tid,)).fetchone()
            if t is None:
                return {"unlinked": False, "error": "no such transmission"}
            old = db.execute("SELECT why, unit FROM radio_links WHERE traffic_id = ? AND incident_id = ?", (tid, iid)).fetchone()
            if old is None:
                return {"unlinked": False, "why": ""}
            db.execute("DELETE FROM radio_links WHERE traffic_id = ? AND incident_id = ?", (tid, iid))
            auto = old["why"] != "manual"
            if auto:
                db.execute("INSERT OR REPLACE INTO radio_link_exclusions (traffic_id, incident_id, at, operator, why, unit) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (tid, iid, self.clock(), operator, old["why"], old["unit"]))
            name = re.sub(r"\s*<[^<>\s]+@[^<>\s]+>$", "", operator).strip()
            self.store._event(db, iid, "radio", f"Radio {_when(t)} {_who(t)} on {t['channel']} removed from this event"
                              + (f" (auto: {why_text(old['why'], old['unit'])})" if auto else "")
                              + (f" by {name}" if name else ""), operator)
            self.rev += 1
        return {"unlinked": True, "why": old["why"], "excluded": auto}

    def retext(self, tid: str, text: str, status: str, model: str = "", alt: dict | None = None) -> bool:
        """New text for a transmission. alt {text, model}: the other engine's transcript, kept to compare
        ("both": Amazon Transcribe's is the text, whisper's the alternative)."""
        with self.store.tx() as db:
            if alt is not None:
                db.execute("UPDATE radio_traffic SET alt_text = ?, alt_model = ? WHERE id = ?",
                           (str(alt.get("text") or ""), str(alt.get("model") or ""), tid))
            n = db.execute("UPDATE radio_traffic SET text = ?, status = ?, model = ?, updated_at = ? WHERE id = ?",
                           (text, status, model, self.clock(), tid)).rowcount
            if n:
                self.rev += 1
        if n:
            self._heard(tid)
        return bool(n)

    def set_alt(self, tid: str, alt: dict) -> bool:
        """The other engine's text ("both"), the transcript left as it is."""
        with self.store.tx() as db:
            n = db.execute("UPDATE radio_traffic SET alt_text = ?, alt_model = ?, updated_at = ? WHERE id = ?",
                           (str(alt.get("text") or ""), str(alt.get("model") or ""), self.clock(), tid)).rowcount
            if n:
                self.rev += 1
        return bool(n)

    def rerun_done(self, tid: str, result: str) -> bool:
        """A re-run (re-transcribe) is over: when, and what came of it (changed | same | failed: why),
        even when the text is the same, so the page that asked sees it end."""
        with self.store.tx() as db:
            n = db.execute("UPDATE radio_traffic SET rerun_at = ?, rerun_result = ?, updated_at = ? WHERE id = ?",
                           (self.clock(), result, self.clock(), tid)).rowcount
            if n:
                self.rev += 1
        return bool(n)

    def prune(self) -> int:
        cutoff = self.clock() - KEEP_UNLINKED_S
        with self.store.tx() as db:
            return db.execute("DELETE FROM radio_traffic WHERE start < ? AND id NOT IN (SELECT traffic_id FROM radio_links)",
                              (cutoff,)).rowcount

    def retag_pages(self, match, is_ours) -> int:
        """Every page row's tone pairs matched again (#80: a pair named from one row is named on them all,
        older unknown rows included). match(a_hz, b_hz) -> names, is_ours(names) -> bool. Rows changed."""
        n = 0
        with self.store.tx() as db:
            for r in db.execute("SELECT id, tones FROM radio_traffic WHERE tones != ''").fetchall():
                try:
                    tones = json.loads(r["tones"])
                except (TypeError, ValueError):
                    continue
                new = []
                for t in tones:
                    names = list(match(float(t.get("a_hz") or 0), t.get("b_hz")))
                    new.append(dict(t, names=names, ours=bool(is_ours(names))))
                if new != tones:
                    db.execute("UPDATE radio_traffic SET tones = ?, updated_at = ? WHERE id = ?", (json.dumps(new), self.clock(), r["id"]))
                    n += 1
        if n:
            self.rev += 1
            self.store.rev += 1
        return n

    # -- reading --------------------------------------------------------------------------------

    def for_incidents(self, ids: list[int], limit: int = 200) -> dict[int, list[dict]]:
        """Each event's radio traffic, newest first, each with every event it is on (events)."""
        out: dict[int, list[dict]] = {i: [] for i in ids}
        if not ids:
            return out
        rows = self.store.q(f"SELECT t.*, l.incident_id, l.why, l.unit AS link_unit FROM radio_links l "
                            f"JOIN radio_traffic t ON t.id = l.traffic_id WHERE l.incident_id IN ({','.join('?' * len(ids))}) "
                            f"ORDER BY t.start DESC", ids)
        for r in rows:
            lst = out[r.pop("incident_id")]
            if len(lst) < limit:
                lst.append(_tones(r, self.paging, self.names_ours))
        shown = [r for lst in out.values() for r in lst]
        ev = self.events_of(list({r["id"] for r in shown}))
        for r in shown:
            r["events"] = ev.get(r["id"], [])
        with_alarms(self.store, shown)
        return out

    def events_of(self, tids: list[str]) -> dict[str, list[dict]]:
        return events_of(self.store, tids)

    def query(self, unit: str = "", channel: str = "", event: str = "", pcfd: list[str] | None = None,
              since: float | None = None, until: float | None = None, limit: int = 300, kind: str = "",
              text: str = "", emergency: bool = False) -> list[dict]:
        """The CAD's Radio view: newest first, with the event numbers each transmission is linked to, and
        (kind '' or 'notes') the event notes tagged with a unit ("E63 stated that ..."), as kind 'note'
        rows on channel "Notes". pcfd: only these rigs (PCFD's own units). text: every word in the
        transcript / note (any case). emergency: only transmissions with the emergency button or an alarm
        (a mayday heard) on them, no notes."""
        limit = max(1, min(int(limit), MAX_ROWS))
        words = [w.lower() for w in text.split()][:8]
        notes = (self._notes(unit, event, pcfd, since, until, limit, words)
                 if kind in ("", "notes") and channel in ("", NOTES) and not emergency else [])
        radio = (self._radio(unit, channel, event, pcfd, since, until, limit, words, emergency)
                 if kind in ("", "radio") and channel != NOTES else [])
        return sorted(radio + notes, key=lambda r: -r["start"])[:limit]

    def _notes(self, unit, event, pcfd, since, until, limit, words=()) -> list[dict]:
        where, args = ["e.kind = 'note'", "e.units != ''"], []
        for w in words:
            where.append("LOWER(e.text) LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(w)}%")
        if unit:
            where.append("(',' || e.units || ',') LIKE ?")
            args.append(f"%,{unit},%")
        if event:
            where.append("(i.number = ? OR CAST(i.id AS TEXT) = ?)")
            args += [event, event]
        if pcfd is not None:
            if not pcfd:
                return []
            where.append("(" + " OR ".join(["(',' || e.units || ',') LIKE ?"] * len(pcfd)) + ")")
            args += [f"%,{u},%" for u in pcfd]
        if since is not None:
            where.append("e.at >= ?")
            args.append(since)
        if until is not None:
            where.append("e.at <= ?")
            args.append(until)
        rows = self.store.q("SELECT e.id, e.at, e.text, e.operator, e.units, i.id AS iid, i.number, i.type, i.subtype, i.address "
                            "FROM events e JOIN incidents i ON i.id = e.incident_id WHERE " + " AND ".join(where)
                            + " ORDER BY e.at DESC LIMIT ?", (*args, limit))
        return [{"kind": "note", "id": f"note-{r['id']}", "start": r["at"], "end": r["at"], "channel": NOTES,
                 "talkgroup": None, "talker": r["operator"], "radio_id": "", "rig": r["units"].replace(",", ", "),
                 "dispatcher": 0, "text": r["text"], "status": "done", "operator": r["operator"],
                 "events": [{"id": r["iid"], "number": r["number"], "type": r["type"], "subtype": r["subtype"],
                             "address": r["address"], "why": "note", "unit": r["units"]}]} for r in rows]

    def _radio(self, unit, channel, event, pcfd, since, until, limit, words=(), emergency=False) -> list[dict]:
        where, args = [], []
        if emergency:
            where.append(EMERGENCY_WHERE if has_alarms(self.store) else "t.emergency = 1")
        for w in words:
            where.append("LOWER(t.text) LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(w)}%")
        if unit:
            where.append("(',' || REPLACE(t.rig, ' ', '') || ',') LIKE ?")
            args.append(f"%,{unit},%")
        if channel:
            where.append("t.channel = ?")
            args.append(channel)
        if event:
            where.append("t.id IN (SELECT l.traffic_id FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                         "WHERE i.number = ? OR CAST(i.id AS TEXT) = ?)")
            args += [event, event]
        if pcfd is not None:
            if not pcfd:
                return []
            where.append("(" + " OR ".join(["(',' || REPLACE(t.rig, ' ', '') || ',') LIKE ?"] * len(pcfd)) + ")")
            args += [f"%,{u},%" for u in pcfd]
        if since is not None:
            where.append("t.start >= ?")
            args.append(since)
        if until is not None:
            where.append("t.start <= ?")
            args.append(until)
        rows = self.store.q("SELECT t.*, 'radio' AS kind FROM radio_traffic t" + (" WHERE " + " AND ".join(where) if where else "")
                            + " ORDER BY t.start DESC LIMIT ?", (*args, limit))
        if rows:
            ev = self.events_of([r["id"] for r in rows])
            for r in rows:
                r["events"] = ev.get(r["id"], [])
                _tones(r, self.paging, self.names_ours)
            with_alarms(self.store, rows)
        return rows

    def facets(self) -> dict:
        return {"channels": [r["channel"] for r in self.store.q(
                    "SELECT channel, MAX(start) AS m FROM radio_traffic GROUP BY channel ORDER BY m DESC")] + [NOTES],
                "rigs": sorted({x.strip() for r in self.store.q("SELECT DISTINCT rig FROM radio_traffic WHERE rig != ''")
                                for x in r["rig"].split(",") if x.strip()})}


def events_of(store, tids: list[str]) -> dict[str, list[dict]]:
    """traffic id -> the events it is on [{id, number, type, subtype, address, status, why, unit}]."""
    ev: dict[str, list[dict]] = {}
    for k in range(0, len(tids), 500):
        part = tids[k:k + 500]
        for l in store.q(f"SELECT l.traffic_id, l.why, l.unit, i.id, i.number, i.type, i.subtype, i.address, i.status "
                         f"FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                         f"WHERE l.traffic_id IN ({','.join('?' * len(part))}) ORDER BY i.id", part):
            ev.setdefault(l.pop("traffic_id"), []).append(l)
    return ev


def evidence_json(ev) -> str:
    """An emergency's raw call record as kept (radio_traffic.evidence): JSON, at most EVIDENCE_MAX (too big: its
    lists left out, the flags kept); '' for none."""
    if not isinstance(ev, dict) or not ev:
        return ""
    try:
        s = json.dumps(ev, separators=(",", ":"))
        if len(s) > EVIDENCE_MAX:
            s = json.dumps({k: v for k, v in ev.items() if not isinstance(v, (list, dict))} | {"trimmed": True},
                           separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    return s if len(s) <= EVIDENCE_MAX else ""


def has_alarms(store) -> bool:
    """The alarms table is there (sizeup/cad/alarms.py makes it when the CAD starts)."""
    return store.one("SELECT 1 AS x FROM sqlite_master WHERE type = 'table' AND name = 'alarms'") is not None


ALARM_COLS = {"alarm_id": None, "alarm_level": "", "alarm_via": "", "ack_by": "", "ack_at": None, "clear_by": "",
              "clear_at": None, "drill": False, "alarm_stuck": ""}


def with_alarms(store, rows: list[dict]) -> list[dict]:
    """Each transmission row with the alarm it raised (one per transmission: alarms.traffic_id is unique):
    alarm_id, alarm_level (alarm | notice), keywords (the words heard), alarm_via (MDC / FleetSync / '' for P25),
    ack_by / ack_at (seen), clear_by / clear_at (all clear), drill, alarm_stuck (its flag turned out stuck, #190: on
    whom) (ALARM_COLS when none); emergency (0/1) from the row or its alarm; evidence as a dict (None when not kept)."""
    got: dict[str, dict] = {}
    tids = [r["id"] for r in rows if r.get("kind", "radio") == "radio"]
    if tids and has_alarms(store):
        cols = {r["name"] for r in store.q("PRAGMA table_info(alarms)")}
        clear = "clear_by, clear_at" if "clear_at" in cols else "'' AS clear_by, NULL AS clear_at"
        clear += ", stuck" if "stuck" in cols else ", '' AS stuck"
        for k in range(0, len(tids), 500):
            part = tids[k:k + 500]
            for a in store.q(f"SELECT traffic_id, id, level, emergency, keywords, via, ack_by, ack_at, {clear}, drill "
                             f"FROM alarms WHERE traffic_id IN ({','.join('?' * len(part))})", part):
                got[a["traffic_id"]] = a
    for r in rows:
        a = got.get(r["id"])
        r.update(ALARM_COLS)
        r["keywords"] = []
        if a is not None:
            try:
                kws = json.loads(a["keywords"] or "[]")
            except (TypeError, ValueError):
                kws = []
            r.update(alarm_id=a["id"], alarm_level=a["level"], keywords=kws if isinstance(kws, list) else [],
                     alarm_via=a["via"] or "", ack_by=a["ack_by"] or "", ack_at=a["ack_at"],
                     clear_by=a["clear_by"] or "", clear_at=a["clear_at"], drill=bool(a["drill"]),
                     alarm_stuck=a["stuck"] or "")
        r["emergency"] = int(bool(r.get("emergency")) or bool(a and a["emergency"]))
        try:
            r["evidence"] = json.loads(r["evidence"]) if r.get("evidence") else None
        except (TypeError, ValueError):
            r["evidence"] = None
    return rows


def why_text(why: str, unit: str = "") -> str:
    """Why an auto link was made, in words: 'E63 assigned', 'FG1, the only call with units' ..."""
    if why == "unit":
        return f"{unit or 'its unit'} assigned"
    if why == "channel":
        return "FG1, the only call with units"
    if why == "dispatcher":
        return f"60 Control answering {unit or 'a unit on it'}"
    if why == "page":
        return "the 60 Control page"
    if why == "airport":
        return "Airport Incident talkgroup, the open airport event"
    return why


def page_kind(tones: list[dict], channel: str = "", paging=()) -> str:
    """What a page's tones say (#80): ours | other | unknown | none; '' for a row that isn't a page. A pair marked
    other_page (an earlier page's tones in the same recording, #183) never makes it ours."""
    if tones:
        if any(t.get("ours") and not t.get("other_page") for t in tones):
            return "ours"
        return "unknown" if any(not t.get("names") for t in tones) else "other"
    return "none" if channel in paging else ""


def _tones(r: dict, paging=(), names_ours=None) -> dict:
    """A row's tone pairs as a list ([] for anything but a page), its page kind (page, #80), every tone heard
    (tone_seq) and, for a page that isn't ours, the units of ours its text names (names_ours, #115)."""
    for k in ("tones", "tone_seq"):
        try:
            r[k] = json.loads(r[k]) if r.get(k) else []
        except (TypeError, ValueError):
            r[k] = []
    r["page"] = page_kind(r["tones"], r.get("channel") or "", paging)
    r["names_ours"] = (names_ours(r.get("text") or "") if names_ours and r["page"] in ("other", "unknown", "none")
                       else [])
    return r


def _when(t) -> str:
    return datetime.fromtimestamp(t["start"], TZ).strftime("%H:%M:%S")


def _who(t) -> str:
    return t["rig"] or t["talker"] or t["radio_id"] or "radio"


def _like(w: str) -> str:
    """A word for LIKE: its own % and _ (and the escape) taken literally."""
    return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
