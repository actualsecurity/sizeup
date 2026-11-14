"""Communications log: every transmission the monitor transcribed, tied to a rig and to CAD events.

    radio_traffic   one row per transmission (the monitor's clip id: its audio plays from
                    /api/audio/<id>.wav while its audio is kept: 12 h, a year on an event, docs/cloud.md
                    "Audio retention"; the transcript is copied here
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
                      mention     the transcript names a unit on the event, its address or its place;
                      fireground  on the event's working fireground channel (sizeup/cad/radiolog.py, #311)
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
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

from sizeup import sourcekeys as sources
from sizeup.cad import airport
from sizeup.cad.radios import is_dispatcher
from sizeup.cad.store import TZ

TURN = re.compile(r"^(?P<call>.+)-t(?P<k>\d+)$")
NOTES = "Notes"          # the /comms "channel" of event notes tagged with a unit
ACTIVITY_RADIOS_S = 7 * 24 * 3600    # radio_activity: which radios keyed is kept this long (the counts longer)
ACTIVITY_KEEP_S = 90 * 24 * 3600     # radio_activity: its per-minute counts kept this long
# Transmissions on no event: their transcripts and metadata kept a year by default (90 days before #414), per channel /
# talkgroup in Settings > System > Audio retention ("transcript_retention"), never shorter than their audio
# ("audio_retention"); linked ones: always; a record (an emergency, a keep, an alarm) as long as the audio bucket keeps
# any clip (its lifecycle hard max; [storage] audio_keep_event_days is at most that).
KEEP_UNLINKED_S = 365 * 24 * 3600
KEEP_RECORD_S = 400 * 24 * 3600
MAX_ROWS = 500
MAIN_LIMIT, OTHER_LIMIT = 200, 300    # the Radio view's Main and Other: each its own newest N (#241)
# The Radio page's views (#415): a named filter each. ours: our fire channels (today's Main); a service's: every source
# of that service; all: everything not muted. One page of PAGE rows (at most PAGE_MAX), older ones by a keyset.
VIEWS = ("ours", "police", "ems", "transit", "air", "all")
SERVICE_VIEWS = ("police", "ems", "transit", "air")
PAGE, PAGE_MAX = 100, 200
SOURCES_MAX, MUTE_MAX = 24, 100       # ?source= and ?mute= keys taken per request
COUNT_MINUTES = 60                    # GET /api/cad/comms/sources: calls per minute over the last hour
SAFETY_WHERE = "t.safety != '' AND t.safety != 'dismissed'"     # as the partial index radio_traffic_safety is declared
BRANCH_MAX = 64                       # a group or tier resolved to its sources: at most this many index walks
CHANGES_MAX = 400                     # ?changed=: more transmissions changed than this since the cursor: reload
CHANGES_KEPT = 50000                  # radio_changes rows kept (prune): a cursor older than those is told to reload
EM_WINDOW_S = 12 * 3600               # the sources' emergency mark: uncleared ones this recent
# Every change to what a row of the Radio page shows, in order (#415 ?changed=): the transmission itself (new, its
# transcript, a re-run, the alternative text, pruned), its links to incidents, a link kept, its alarm (raised, seen,
# cleared), and a note tagged with a unit ('note-...').
#
# The radio log is the record and this log is a convenience, so NOTHING on the write path depends on it: no trigger,
# no retry. A write site says which row it changed (Comms.touch, inside its transaction); the entries are written
# only after that transaction has committed, as their own statement in their own try (Comms.flush_changes, called by
# the store's tx()). Rolled back: the notes are dropped with it. A failure to write an entry is caught, logged once,
# and marks the log broken (every ?changed= answers reload) until an entry can be written again; it is never raised
# and never repeats a write. A write site that forgets to touch costs a late update on the page (its 30 s page is
# the backstop), never a row: tests/test_comms_changes.py walks every writer and fails for one that doesn't.
#
# seq only grows and SQLite has one writer at a time, so a reader that has seen seq N has seen everything up to N.
# system: the row's own, read when the entry is written (given by the writer for a row it deleted). A reader who may
# not see another P25 system's traffic is never told that one of its rows changed or went (its id says its
# talkgroup, time and frequency). NULL: not known: nobody's but a reader's who may see everything.
# oseq: a second sequence over only the entries every reader may know of (ours, a note, a reset; NULL for another
# system's or an unknown one's). Such a reader's cursor, its bounds and the trim of those entries are all in oseq, so
# nothing they are given or told (a cursor, its gaps, reload or not for any cursor) moves with another system's traffic.
CHANGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  traffic_id TEXT NOT NULL,
  system TEXT,                          -- the transmission's system (sizeup/sourcekeys.py); '' a note
  gone INTEGER NOT NULL DEFAULT 0,      -- 1: the transmission was deleted
  oseq INTEGER                          -- every reader's own sequence; NULL: not every reader's to know of
);
CREATE INDEX IF NOT EXISTS radio_changes_id ON radio_changes(traffic_id);
CREATE INDEX IF NOT EXISTS radio_changes_oseq ON radio_changes(oseq) WHERE oseq IS NOT NULL;
"""
# the newest seq and oseq (each by its own key: oseq's index is partial, only the entries that have one)
TOPS = ("SELECT (SELECT MAX(seq) FROM radio_changes), "
        "(SELECT MAX(oseq) FROM radio_changes WHERE oseq IS NOT NULL)")
CURSOR = re.compile(r"(?P<kind>[so])(?P<n>\d{1,15})-(?P<run>[0-9a-f]{8})")
INSERT_CHANGE = ("INSERT INTO radio_changes (traffic_id, system, gone, oseq) VALUES (?, ?, ?, "
                 "CASE WHEN ? THEN (SELECT IFNULL(MAX(oseq), 0) + 1 FROM radio_changes WHERE oseq IS NOT NULL) END)")
# The triggers an earlier build of this branch hung on the tables: taken off at every start
OLD_CHANGE_TRIGGERS = tuple(f"radio_changes_{t}_{w}" for t, ws in (("t", "ins upd del"), ("l", "ins upd del"), ("k", "ins del"),
                                                                    ("n", "ins upd del"), ("a", "ins upd del")) for w in ws.split())
CHANGES_RETRY_S = 60.0                # a broken change log is tried again after this long
CHANGES_BUSY_MS = 200                 # the change log waits this long for the database's write lock, no more
CHANGES_TRIM_EVERY = 500              # the change log is trimmed to CHANGES_KEPT every this many transmissions logged
ID_NOTES = ("none", "undecoded", "weak")    # why an FG1 key-up has no radio ID (radio_traffic.id_note, #163)
RELINK_MAX = 2000                           # a radio just named: at most this many of its unnamed transmissions
EVIDENCE_MAX = 64 * 1024                    # an emergency's raw call record (radio_traffic.evidence), at most
# A unit filter (or PCFD only, #284): the rows it matches are looked up once on the partial index radio_traffic_rig; up to
# this many, each section asks for just those rows (a rare unit no longer reads the whole log to its end); more (a
# busy unit, or every row with a rig): the newest-first walk, which finds enough of them quickly.
RIG_ROWS_MAX = 3000
RIG_LIKE = "(',' || REPLACE({col}, ' ', '') || ',') LIKE ?"
# The Radio view's choices (Comms.facets), a skip along an index each: channel (radio_traffic_channel_start) and rig
# (radio_traffic_rig), one look-up per value.
CHANNELS_SQL = ("WITH RECURSIVE c(ch) AS (SELECT MIN(channel) FROM radio_traffic UNION ALL "
                "SELECT (SELECT MIN(channel) FROM radio_traffic WHERE channel > c.ch) FROM c WHERE c.ch IS NOT NULL) "
                "SELECT ch AS channel, (SELECT MAX(start) FROM radio_traffic WHERE channel = c.ch) AS m FROM c "
                "WHERE ch IS NOT NULL ORDER BY m DESC")
RIGS_SQL = ("WITH RECURSIVE g(rig) AS (SELECT MIN(rig) FROM radio_traffic WHERE rig != '' UNION ALL "
            "SELECT (SELECT MIN(rig) FROM radio_traffic WHERE rig != '' AND rig > g.rig) FROM g WHERE g.rig IS NOT NULL) "
            "SELECT rig FROM g WHERE rig IS NOT NULL")


def rig_rows_sql(n: int) -> str:
    """The rows whose rig names any of n units (n LIKE patterns), at most RIG_ROWS_MAX + 1: on radio_traffic_rig."""
    return ("SELECT rowid AS r FROM radio_traffic WHERE rig != '' AND (" + " OR ".join([RIG_LIKE.format(col="rig")] * n)
            + f") LIMIT {RIG_ROWS_MAX + 1}")
# A transmission that had the emergency button or raised an alarm (the Radio view's Emergency filter)
EMERGENCY_WHERE = "(t.emergency = 1 OR t.id IN (SELECT traffic_id FROM alarms))"
# Links the radio log made from words alone (#311, sizeup/cad/radiolog.py): they don't keep a transmission's audio or row
# past the normal retention until a person keeps them (radio_link_kept).
AUTO_UNSURE = ("mention", "fireground")
KEPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_link_kept (
  traffic_id TEXT NOT NULL,
  incident_id INTEGER NOT NULL,
  at REAL NOT NULL,
  operator TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (traffic_id, incident_id)
);
-- Activity per source per minute (#409, #415): an encrypted talkgroup's key-ups are only ever here (never a
-- transmission row: nothing of it is recorded), with the radio IDs that keyed; every other source's are counted too.
CREATE TABLE IF NOT EXISTS radio_activity (
  source TEXT NOT NULL,                 -- p25:<system>:<tg> | fm:<label> | air:<label> (sizeup/sourcekeys.py)
  minute INTEGER NOT NULL,              -- unix time // 60
  calls INTEGER NOT NULL DEFAULT 0,
  seconds REAL NOT NULL DEFAULT 0,
  radios TEXT NOT NULL DEFAULT '',      -- radio IDs heard that minute, comma-separated (encrypted sources)
  PRIMARY KEY (source, minute)
);
CREATE INDEX IF NOT EXISTS radio_activity_minute ON radio_activity(minute);
"""
# A link that keeps its transmission: any but an unconfirmed mention / fireground one
SURE_LINK = ("(l.why NOT IN ('mention', 'fireground') OR EXISTS (SELECT 1 FROM radio_link_kept k "
             "WHERE k.traffic_id = l.traffic_id AND k.incident_id = l.incident_id))")


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
        # Another P25 system's rows (#409) are only ever read out for a signed-in person (sourcekeys.may_see_context):
        # True here only when the server verifies every reader (the hub behind Access), for the state all share
        self.context_broadcast = False
        # The airband rows' ATC talker (sizeup/atc_talkers.py AtcTalkers.decorate, #252): set by the CAD; rows in place
        self.atc = None
        self.fix = None                 # rows -> rows: the status hints' decorate (sizeup/cad/statushints.py)
        self.names_ours = None  # text -> [our unit codes] (the feed's matcher, #115): a page not ours naming them
        self.on_text = None     # traffic id -> None: a transcript arrived or changed (status suggestions, #37)
        # traffic id -> None: a transcript arrived or changed on another P25 system's row (context only; the intel
        # pre-alerts); never called for our rows. Called after the transaction has committed, never under store.lock.
        # May fire again for the same tid (replace re-records, the startup backfill of old rows): a consumer dedupes
        # by tid + text, ignores a stale `start`, and queues rather than blocks (it runs on the writer's thread).
        self.on_context_text = None
        self._revs: dict = {}   # name -> (rev, value): what _per_rev made
        self.changes_broken = False      # an entry couldn't be written: every ?changed= answers reload (flush_changes)
        self.changes_floor = 0           # cursors below this are told to reload (the log has a gap before it)
        self.changes_floor_o = 0         # the same in oseq (a reader who may not see another system's traffic)
        self._quiet = False              # the open transaction wrote only another system's data (quiet())
        self._tc = 0                     # the connection's total_changes when the last transaction was told of
        self._run = os.urandom(4).hex()  # this run, in every change cursor (cursor())
        self._trimmed = self._trimmed_o = 0     # the newest seq / oseq trimmed from the log (a cursor below: reload)
        self._pending: list = []         # (id, system | None, gone) touched in the open transaction
        self._retry_at = 0.0             # a broken log: when to try writing to it again
        self._said_broken = False
        self.ours_rev = 0                # changes every reader may know of (rev_for)
        self.log = lambda m: None        # set by the CAD
        self._logged = {True: 0, False: 0}   # transmissions logged since the log was last trimmed, each kind (_everyones)
        with self.store.lock:
            self.store.db.executescript(KEPT_SCHEMA)
        self._open_changes()
        self._drop_channel_links()

    def _drop_channel_links(self) -> None:
        """The links the old FG1 channel rule made (an unidentified radio put on the one call with units): gone
        (#163). A person's own (manual) links stay."""
        try:
            with self.store.tx() as db:
                if db.execute("DELETE FROM radio_links WHERE why = 'channel'").rowcount:
                    self.touch_reset()
        except Exception:
            pass                # no comms tables yet (a fresh store migrates first)

    # -- writing --------------------------------------------------------------------------------

    def record(self, t: dict, replace: bool = True) -> list[int]:
        """One transmission: {id, start, end, source, talkgroup, unit (talker as heard), rig, radio_id,
        text, status, model, emergency?, evidence?}. Returns the event ids it is linked to. replace=False: skip
        one already logged (the startup backfill). An emergency once logged stays (a later write without the
        flag never clears it), and so does its evidence. Must not be called with store.lock held: the hooks
        (on_text, on_context_text) run on the caller's thread after commit."""
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
            mhz = t.get("mhz")                  # the frequency heard on (an airband row, #260); kept when not sent again
            mhz = round(float(mhz), 4) if isinstance(mhz, (int, float)) and not isinstance(mhz, bool) and mhz > 0 else None
            # its source, by its system (#409 / #415: sizeup/sourcekeys.py); another P25 system's is context only
            system = str(t.get("system") or "")
            other = sources.other_p25({"talkgroup": t.get("talkgroup"), "system": system})
            skey = str(t.get("source_key") or "") or sources.of_row(t.get("source") or "", t.get("talkgroup"), system,
                                                                    air=mhz is not None)
            system = system or (sources.COUNTY if p25 else "airband" if mhz is not None else "analog")
            # no service given (a caller from before #415): an airband row is air, an analog channel is ours: fire
            given = str(t.get("service") or "") or ("" if p25 else "air" if mhz is not None else "fire")
            src = (skey, system, str(t.get("group") or "")[:60], given[:16],
                   int(bool(t.get("ours")) and not other), str(t.get("dept") or "")[:60], str(t.get("tier") or "")[:16])
            disp = 0 if other else disp          # "60 Ctrl ..." is the county's dispatcher, never another system's
            rigs = [] if other else rigs
            vals = (call_id, turn, start, float(t.get("end") or start), t.get("source") or "", t.get("talkgroup"),
                    talker, t.get("radio_id") or "", ", ".join(rigs), disp, t.get("text") or "", t.get("status") or "",
                    t.get("model") or "", tones, t.get("voice_at"), seq, note, int(bool(t.get("emergency"))),
                    str(t.get("kept") or "")[:40], ev, mhz, now)
            if have:
                db.execute("UPDATE radio_traffic SET call_id = ?, turn = ?, start = ?, end = ?, channel = ?, talkgroup = ?, "
                           "talker = ?, radio_id = ?, rig = ?, dispatcher = ?, text = ?, status = ?, model = ?, tones = ?, "
                           "voice_at = ?, tone_seq = ?, id_note = ?, emergency = MAX(emergency, ?), "
                           "kept = COALESCE(NULLIF(?, ''), kept), evidence = COALESCE(NULLIF(?, ''), evidence), "
                           "mhz = COALESCE(?, mhz), updated_at = ? WHERE id = ?", (*vals, tid))
                db.execute('UPDATE radio_traffic SET source = ?, system = ?, "group" = ?, service = ?, ours = ?, dept = ?, '
                           "tier = ? WHERE id = ?", (*src, tid))
            else:
                db.execute("INSERT INTO radio_traffic (call_id, turn, start, end, channel, talkgroup, talker, radio_id, rig, "
                           "dispatcher, text, status, model, tones, voice_at, tone_seq, id_note, emergency, kept, evidence, "
                           "mhz, updated_at, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (*vals, tid))
                db.execute('UPDATE radio_traffic SET source = ?, system = ?, "group" = ?, service = ?, ours = ?, dept = ?, '
                           "tier = ? WHERE id = ?", (*src, tid))
                self._count(db, skey, start, max(0.0, float(t.get("end") or start) - start))
            self.touch(tid)
            if not other:
                # Another system's units never put traffic on our incidents by a rule (#415): no unit, airport or
                # dispatcher link. Only a person's own link does (link()).
                self._rule_links(db, tid, call_id, turn, p25, known, start, rigs, t.get("talkgroup"), bool(disp))
            self.rev += 1
            out = sorted(r[0] for r in db.execute("SELECT incident_id FROM radio_links WHERE traffic_id = ?", (tid,)))
        if other:
            if (t.get("text") or "").strip():
                self._heard_context(tid)            # committed: the intel pre-alerts, never our consumers (on_text)
            return out
        kind = self._everyones(tid, system)              # each kind trimmed by its own count: never by the other's
        self._logged[kind] += 1
        if self._logged[kind] >= CHANGES_TRIM_EVERY:
            self._logged[kind] = 0
            self._trim_changes(kind)
        self._heard(tid)
        return out

    def _rule_links(self, db, tid: str, call_id: str, turn, p25: bool, known: bool, start: float, rigs: list[str],
                    tg, disp: bool) -> None:
        """record()'s automatic links for one of our rows: a unit on the event, the airport talkgroup, dispatch."""
        off = {r[0] for r in db.execute("SELECT incident_id FROM radio_link_exclusions WHERE traffic_id = ?", (tid,))}
        links: dict[int, tuple[str, str]] = {}
        for rig in rigs:
            for r in db.execute(self.ON_EVENT_BY_UNIT, (rig, start, start)).fetchall():
                links.setdefault(r["incident_id"], ("unit", rig))
        # No ID, no link (#163): an unidentified FG1 radio stays in the Radio view, whoever's call is running.
        if p25 and known and tg in self.airport_tgs:
            open_ap = db.execute(self.OPEN_AIRPORT, (airport.TYPE, start, start)).fetchall()
            if len(open_ap) == 1:           # the Airport Incident talkgroup: the one open airport event's
                links.setdefault(open_ap[0]["id"], ("airport", ""))
        for iid, (why, unit) in links.items():
            if iid not in off:              # a person took it off that event: not again
                self._auto(db, tid, iid, why, unit)
        if p25 and turn is not None and tg in self.incident_tgs:
            self._dispatcher_links(db, tid, call_id, turn, disp, links)

    @staticmethod
    def _count(db, source: str, at: float, seconds: float = 0.0, calls: int = 1, radios=()) -> None:
        """radio_activity: one more call on this source in the minute of `at`."""
        ids = ",".join(dict.fromkeys(str(x) for x in radios if x))
        cur = db.execute("SELECT radios FROM radio_activity WHERE source = ? AND minute = ?", (source, int(at // 60))).fetchone()
        if cur is None:
            db.execute("INSERT INTO radio_activity (source, minute, calls, seconds, radios) VALUES (?, ?, ?, ?, ?)",
                       (source, int(at // 60), calls, seconds, ids[:400]))
            return
        both = ",".join(dict.fromkeys([x for x in cur[0].split(",") if x] + [x for x in ids.split(",") if x]))
        db.execute("UPDATE radio_activity SET calls = calls + ?, seconds = seconds + ?, radios = ? WHERE source = ? "
                   "AND minute = ?", (calls, seconds, both[:400], source, int(at // 60)))

    def fix_air(self, labels) -> int:
        """Rows of the airband channels logged as analog (from before a row said which it was: no frequency on them):
        their source, system and service put right, and never "ours". The number changed."""
        labels = [str(x) for x in labels if x]
        if not labels:
            return 0
        with self.store.tx() as db:
            self.touch_reset()
            return db.execute("UPDATE radio_traffic SET source = 'air:' || channel, system = 'airband', service = 'air', "
                              f"ours = 0 WHERE talkgroup IS NULL AND system != 'airband' AND channel IN "
                              f"({','.join('?' * len(labels))})", labels).rowcount

    def activity(self, source: str, minute: int, calls: int, radios=(), seconds: float = 0.0) -> None:
        """Key-ups on a source that leaves no transmission rows (an encrypted talkgroup, #409): {source, minute,
        calls, radios} as the station counted them, added to that minute."""
        if not sources.is_talkgroup(source) or calls <= 0:
            return
        with self.store.tx() as db:
            self._count(db, source, minute * 60.0, seconds, int(calls), radios)
            if sources.is_context({"system": (sources.parse(source) or ("", 0))[0]}):
                self.quiet()                             # another system's key-ups: no reader's rev but theirs moves

    def activity_rows(self, since: float, source: str = "", limit: int = 2000) -> list[dict]:
        """[{source, minute, calls, seconds, radios}] since then, newest first (GET /api/cad/comms/activity, admins)."""
        return self.store.q("SELECT source, minute, calls, seconds, radios FROM radio_activity WHERE minute >= ? "
                            + ("AND source = ? " if source else "") + "ORDER BY minute DESC LIMIT ?",
                            (int(since // 60), *([source] if source else []), int(limit)))

    def audio_per_day(self, days: float = 7.0) -> dict[str, float]:
        """Seconds of audio logged per day, per tier, over the last `days` (the expected storage, sizeup/tiers.py)."""
        since = self.clock() - days * 86400
        first = self.store.one("SELECT MIN(start) AS a FROM radio_traffic WHERE tier != '' AND start >= ?", (since,))
        span = max(3600.0, self.clock() - (first["a"] if first and first["a"] else since)) / 86400
        return {r["tier"]: (r["s"] or 0.0) / span for r in self.store.q(
            "SELECT tier, SUM(end - start) AS s FROM radio_traffic WHERE tier != '' AND start >= ? GROUP BY tier", (since,))}

    def context_clips(self, ids: list[str], days: dict[str, float]) -> dict[str, tuple[int, bool]]:
        """Of these clip ids, another P25 system's (#409): {id: (rank, extended)}. rank: what goes first when its audio
        has to (record 0, text 1, alert 2); extended: its tier's retention still keeps it. A clip on a CAD event, with
        an alarm or an emergency is a record like any other, not context: left out (kept_for_events)."""
        rank = {"record": 0, "text": 1, "alert": 2}
        tiered: dict[str, str] = {}
        for k in range(0, len(ids), 500):
            chunk = list(ids[k:k + 500])
            tiered.update((r["id"], r["tier"]) for r in self.store.q(
                f"SELECT id, tier FROM radio_traffic WHERE id IN ({','.join('?' * len(chunk))}) AND system NOT IN "
                "('', 'analog', 'airband', ?) AND talkgroup IS NOT NULL", (*chunk, sources.COUNTY)))
        if not tiered:
            return {}
        record = self.kept_for_events(list(tiered))
        ext = self.kept_for_tier([i for i in tiered if i not in record], days)
        return {i: (rank.get(t, 0), i in ext) for i, t in tiered.items() if i not in record}

    def kept_for_tier(self, ids: list[str], days: dict[str, float]) -> set[str]:
        """Of these clip ids, the ones a tier's own retention still keeps ([storage] tier_keep_days, sizeup/tiers.py)."""
        out: set[str] = set()
        now = self.clock()
        keep = {t: d for t, d in days.items() if d and d > 0}
        for k in range(0, len(ids), 500):
            chunk = list(ids[k:k + 500])
            for tier, d in keep.items():
                out.update(r["id"] for r in self.store.q(
                    f"SELECT id FROM radio_traffic WHERE id IN ({','.join('?' * len(chunk))}) AND tier = ? AND start >= ?",
                    (*chunk, tier, now - d * 86400)))
        return out

    def _heard(self, tid: str) -> None:
        if self.on_text is not None:
            try:
                self.on_text(tid)
            except Exception as e:  # noqa: BLE001 - a suggestion never stops the log
                print(f"CAD: status suggestion for {tid}: {type(e).__name__}: {e}")

    def _heard_context(self, tid: str) -> None:
        """on_context_text, once the write has committed: whatever it does or raises never reaches the write."""
        if self.on_context_text is not None:
            try:
                self.on_context_text(tid)
            except Exception as e:  # noqa: BLE001 - a pre-alert never stops the log
                self.log(f"CAD: context transcript {tid}: {type(e).__name__}: {e}")

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

    def _auto(self, db, tid: str, iid: int, why: str, unit: str) -> None:
        """An auto link, unless a person took this transmission off that event (radio_link_exclusions)."""
        db.execute("INSERT OR IGNORE INTO radio_links (traffic_id, incident_id, why, unit) SELECT ?, ?, ?, ? "
                   "WHERE NOT EXISTS (SELECT 1 FROM radio_link_exclusions WHERE traffic_id = ? AND incident_id = ?)",
                   (tid, iid, why, unit, tid, iid))
        self.touch(tid)

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
                              + ("talkgroup IS NULL" if fm else "talkgroup IS NOT NULL AND system IN ('', ?)")
                              + " AND (', ' || radio_id || ', ') LIKE ? ORDER BY start DESC LIMIT ?",
                              (*([] if fm else [sources.COUNTY]), f"%, {rid}, %", RELINK_MAX)).fetchall()
            for r in rows:
                db.execute("UPDATE radio_traffic SET rig = ?, id_note = '', updated_at = ? WHERE id = ?",
                           (unit, self.clock(), r["id"]))
                self.touch(r["id"])
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
                self.touch(pid)
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
            self.touch(tid)
            db.execute("DELETE FROM radio_link_exclusions WHERE traffic_id = ? AND incident_id = ?", (tid, iid))
            db.execute("INSERT INTO radio_links (traffic_id, incident_id, why, unit) VALUES (?, ?, 'manual', ?)",
                       (tid, iid, operator))
            # (another P25 system's row, #419: when and which channel only; the timeline is read without a sign-in)
            said = "" if sources.is_context(t) else (t["text"] or "").strip()
            self.store._event(db, iid, "radio", f"Radio traffic added: {_row_line(t)}"
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
            self.touch(tid)
            db.execute("DELETE FROM radio_links WHERE traffic_id = ? AND incident_id = ?", (tid, iid))
            db.execute("DELETE FROM radio_link_kept WHERE traffic_id = ? AND incident_id = ?", (tid, iid))
            auto = old["why"] != "manual"
            if auto:
                db.execute("INSERT OR REPLACE INTO radio_link_exclusions (traffic_id, incident_id, at, operator, why, unit) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (tid, iid, self.clock(), operator, old["why"], old["unit"]))
            name = re.sub(r"\s*<[^<>\s]+@[^<>\s]+>$", "", operator).strip()
            self.store._event(db, iid, "radio", f"Radio {_row_line(t)} removed from this event"
                              + (f" (auto: {why_text(old['why'], old['unit'])})" if auto else "")
                              + (f" by {name}" if name else ""), operator)
            self.rev += 1
        return {"unlinked": True, "why": old["why"], "excluded": auto}

    def retext(self, tid: str, text: str, status: str, model: str = "", alt: dict | None = None) -> bool:
        """New text for a transmission. alt {text, model}: the other engine's transcript, kept to compare
        ("both": Amazon Transcribe's is the text, whisper's the alternative). Another P25 system's row goes to
        on_context_text only, never to on_text (as in record()). Must not be called with store.lock held: the hooks
        run on the caller's thread after commit."""
        with self.store.tx() as db:
            self.touch(tid)
            if alt is not None:
                db.execute("UPDATE radio_traffic SET alt_text = ?, alt_model = ? WHERE id = ?",
                           (str(alt.get("text") or ""), str(alt.get("model") or ""), tid))
            n = db.execute("UPDATE radio_traffic SET text = ?, status = ?, model = ?, updated_at = ? WHERE id = ?",
                           (text, status, model, self.clock(), tid)).rowcount
            row = db.execute("SELECT system, talkgroup FROM radio_traffic WHERE id = ?", (tid,)).fetchone() if n else None
            if n:
                self.rev += 1
        if n:
            if row is not None and sources.other_p25(row):
                self._heard_context(tid)
            else:
                self._heard(tid)
        return bool(n)

    def set_stt(self, tid: str, status: str, model: str) -> bool:
        """Where a transmission's speech-to-text stands, its text left as it is: the status ("pending", "error") and
        the engine doing it or that failed (model), so the log never says "Transcribing on the hub (AWS)" for a clip
        whisper took over from AWS, or one that failed. Only a row still waiting (pending or remote, no text): a text
        that came first (a fast whisper, a late AWS answer) is never put back to waiting or failed."""
        with self.store.tx() as db:
            self.touch(tid)
            n = db.execute("UPDATE radio_traffic SET status = ?, model = ?, updated_at = ? WHERE id = ? "
                           "AND status IN ('pending', 'remote') AND text = ''",
                           (status, model, self.clock(), tid)).rowcount
            if n:
                self.rev += 1
        return bool(n)

    def set_alt(self, tid: str, alt: dict) -> bool:
        """The other engine's text ("both"), the transcript left as it is."""
        with self.store.tx() as db:
            self.touch(tid)
            n = db.execute("UPDATE radio_traffic SET alt_text = ?, alt_model = ?, updated_at = ? WHERE id = ?",
                           (str(alt.get("text") or ""), str(alt.get("model") or ""), self.clock(), tid)).rowcount
            if n:
                self.rev += 1
        return bool(n)

    def rerun_done(self, tid: str, result: str) -> bool:
        """A re-run (re-transcribe) is over: when, and what came of it (changed | same | failed: why),
        even when the text is the same, so the page that asked sees it end."""
        with self.store.tx() as db:
            self.touch(tid)
            n = db.execute("UPDATE radio_traffic SET rerun_at = ?, rerun_result = ?, updated_at = ? WHERE id = ?",
                           (self.clock(), result, self.clock(), tid)).rowcount
            if n:
                self.rev += 1
        return bool(n)

    def _section(self, name: str) -> dict:
        out = {}
        for r in self.store.q("SELECT key, value FROM settings_overrides WHERE section = ?", (name,)):
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                continue
        return out

    # The audio's default days (the hub's [storage] audio_keep_hours / 24, set by sizeup/cloud/hub.py) and the floor of
    # a record's. None until the hub says: then the audio counts as kept as long as Settings allows (365 days), so
    # a prune never takes a transcript before its audio (the prune at start runs before the hub has said).
    audio_default_days: float | None = None
    PRUNE_BATCH = 500

    def policies(self):
        """(the transcripts' retention, the audio's) from Settings > System > Audio retention (#414,
        sizeup/audiostore.py): the transcripts default KEEP_UNLINKED_S, the audio the hub's default."""
        from sizeup import audiostore
        a_default = self.audio_default_days if self.audio_default_days is not None else audiostore.RETENTION_DAYS[1]
        return (audiostore.Retention(self._section(audiostore.TRANSCRIPT_SECTION), KEEP_UNLINKED_S / 86400),
                audiostore.Retention(self._section(audiostore.RETENTION_SECTION), a_default))

    def _groups(self, tpol, apol) -> list[tuple[str, tuple, float]]:
        """(an SQL condition, its parameters, the days kept) covering every row once: per talkgroup with days of its
        own (in either policy) by channel with days of its own, per such channel, the rest: each kept the longer of its
        transcripts' and its audio's days, by the same precedence as the prunes (talkgroup > channel > default)."""
        tgs = sorted(set(tpol.tg) | set(apol.tg))
        chs = sorted(set(tpol.ch) | set(apol.ch))
        days = lambda ch, tg: max(tpol.keep_days(ch, tg), apol.keep_days(ch, tg))   # noqa: E731
        no_ch = f"channel NOT IN ({','.join('?' * len(chs))})" if chs else "1"
        # "tg:<n>" is ours: another P25 system's row (#409, the same number is another talkgroup there) goes by its channel
        ours = f"system IN {sources.OURS_SQL}"
        no_tg = (f"(talkgroup IS NULL OR NOT {ours} OR talkgroup NOT IN ({','.join('?' * len(tgs))}))"
                 if tgs else "1")
        out = []
        for tg in tgs:
            for ch in chs:
                out.append((f"talkgroup = ? AND {ours} AND channel = ?", (tg, ch), days(ch, tg)))
            out.append((f"talkgroup = ? AND {ours} AND {no_ch}", (tg, *chs), days("", tg)))
        for ch in chs:
            out.append((f"channel = ? AND {no_tg}", (ch, *tgs), days(ch, None)))
        out.append((f"{no_tg} AND {no_ch}", (*tgs, *chs), days("", None)))
        return out

    def prune(self, pause_s: float = 0.0) -> int:
        """Transmissions on no event, once older than the longer of their transcripts' retention (Settings, per channel /
        talkgroup, 365 days by default) and their audio's: a transcript never goes before its audio. A record
        (emergency, kept, an alarm) not before KEEP_RECORD_S either; one linked to an event, never. One indexed query
        per channel / talkgroup group ((talkgroup, start), (channel, start), (start)), deleted PRUNE_BATCH rows a
        transaction (the CAD's write lock held only that long), pause_s apart. The number deleted."""
        now = self.clock()
        with self.store.tx() as db:                    # radio_activity (encrypted talkgroups' counts): its own ages
            old, aged = int((now - ACTIVITY_KEEP_S) // 60), int((now - ACTIVITY_RADIOS_S) // 60)
            going = [r[0] for r in db.execute(
                "SELECT DISTINCT source FROM radio_activity WHERE minute < ? OR (radios != '' AND minute < ?)", (old, aged))]
            db.execute("DELETE FROM radio_activity WHERE minute < ?", (old,))
            db.execute("UPDATE radio_activity SET radios = '' WHERE radios != '' AND minute < ?", (aged,))
            if not any(self._everyones("", (sources.parse(k) or ("", 0))[0]) for k in going):
                self.quiet()                           # only another system's minutes went: no other reader's rev moves
        tpol, apol = self.policies()
        alarms = bool(self.store.q("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alarms'"))
        record = "emergency = 1 OR kept != ''" + (" OR id IN (SELECT traffic_id FROM alarms)" if alarms else "")
        n = 0
        noted = {True: 0, False: 0}                    # rows noted one by one, each kind; past CHANGES_MAX: that kind's reset
        for cond, params, days in self._groups(tpol, apol):
            cutoff, after = now - days * 86400, float("-inf")
            while True:
                rows = self.store.q(
                    f"SELECT rowid AS r, id, system, start FROM radio_traffic WHERE {cond} AND start < ? AND start >= ? "
                    f"AND id NOT IN (SELECT l.traffic_id FROM radio_links l WHERE {SURE_LINK}) "
                    f"AND NOT (start >= ? AND ({record})) ORDER BY start LIMIT ?",
                    (*params, cutoff, after, now - KEEP_RECORD_S, self.PRUNE_BATCH))
                if not rows:
                    break
                with self.store.tx() as db:            # the conditions again: a link confirmed since the look keeps it
                    for r in rows:
                        gone = db.execute(f"DELETE FROM radio_traffic WHERE rowid = ? AND NOT EXISTS (SELECT 1 FROM "
                                          f"radio_links l WHERE l.traffic_id = radio_traffic.id AND {SURE_LINK}) "
                                          f"AND NOT (start >= ? AND ({record}))",
                                          (r["r"], now - KEEP_RECORD_S)).rowcount
                        if not gone:
                            continue
                        n += 1
                        kind = self._everyones(r["id"], r["system"])
                        noted[kind] += 1
                        if noted[kind] <= CHANGES_MAX:  # (the change log: written after the commit, store.after_commit)
                            self.touch(r["id"], r["system"], gone=True)
                        elif noted[kind] == CHANGES_MAX + 1:
                            # a big clear-out: the pages ask afresh; of another system's rows: only the readers who may
                            # see them (that reset is theirs, so how many went is never told to anyone else)
                            self.touch_reset() if kind else self.touch("reset", r["system"])
                after = rows[-1]["start"]
                if len(rows) < self.PRUNE_BATCH:
                    break
                if pause_s:
                    time.sleep(pause_s)
        self._trim_changes()
        return n

    def kept_for_events(self, ids: list[str]) -> set[str]:
        """Of these clip ids, the ones whose audio is a record (the hub's audio retention, #251): on a CAD event
        (radio_links, any why but an unconfirmed mention / fireground one, #311), with an alarm, or with the emergency button or an emergency window's keep."""
        out: set[str] = set()
        for k in range(0, len(ids), 500):
            chunk = list(ids[k:k + 500])
            marks = ",".join("?" * len(chunk))
            sql = (f"SELECT l.traffic_id AS id FROM radio_links l WHERE l.traffic_id IN ({marks}) AND {SURE_LINK} "
                   f"UNION SELECT id FROM radio_traffic WHERE id IN ({marks}) AND (emergency = 1 OR kept != '')")
            out.update(r["id"] for r in self.store.q(sql, chunk * 2))
            try:
                out.update(r["id"] for r in self.store.q(f"SELECT traffic_id AS id FROM alarms WHERE traffic_id IN ({marks})", chunk))
            except Exception:  # (a CAD without the alarms table yet)
                pass
        return out

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
                    self.touch(r["id"])
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
        rows = self.store.q(f"SELECT t.*, l.incident_id, l.why, l.unit AS link_unit, EXISTS (SELECT 1 FROM radio_link_kept k "
                            f"WHERE k.traffic_id = l.traffic_id AND k.incident_id = l.incident_id) AS link_kept FROM radio_links l "
                            f"JOIN radio_traffic t ON t.id = l.traffic_id WHERE l.incident_id IN ({','.join('?' * len(ids))}) "
                            + ("" if self._sees_context() else f"AND t.system IN {sources.OURS_SQL} ")
                            + "ORDER BY t.start DESC", ids)
        for r in rows:
            lst = out[r.pop("incident_id")]
            if len(lst) < limit:
                lst.append(_tones(r, self.paging, self.names_ours))
        shown = [r for lst in out.values() for r in lst]
        ev = self.events_of(list({r["id"] for r in shown}))
        for r in shown:
            r["events"] = ev.get(r["id"], [])
        with_alarms(self.store, shown)
        if self.atc is not None:
            self.atc(shown)
        if self.fix is not None:
            self.fix(shown)
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

    def split(self, main_tgs, not_main=(), unit: str = "", channel: str = "", event: str = "", pcfd: list[str] | None = None,
              since: float | None = None, until: float | None = None, main_limit: int = MAIN_LIMIT, limit: int = OTHER_LIMIT,
              kind: str = "", text: str = "", emergency: bool = False) -> tuple[list[dict], list[dict]]:
        """The Radio view's two sections, each its own newest N with the same filters (#241: on a busy evening the
        other talkgroups filled the shared newest 300 and Main showed None): (main, other), newest first.
        Main: the FM channels (no talkgroup) but not_main (the airband, #233), the talkgroups in main_tgs and the
        notes, at most main_limit; Other: every other transmission, at most limit. Main asks once per talkgroup
        (and once for the FM channels) on radio_traffic_tg_start, so a quiet Main is found under any volume of
        other traffic; Other walks radio_traffic_start (or radio_traffic_channel_start, one channel asked)."""
        main_limit = max(1, min(int(main_limit), MAX_ROWS))
        limit = max(1, min(int(limit), MAX_ROWS))
        words = [w.lower() for w in text.split()][:8]
        main_tgs, not_main = sorted({int(t) for t in main_tgs}), tuple(dict.fromkeys(not_main))
        notes = (self._notes(unit, event, pcfd, since, until, main_limit, words)
                 if kind in ("", "notes") and channel in ("", NOTES) and not emergency else [])
        main, other = [], []
        w = self._radio_where(unit, channel, event, pcfd, since, until, words, emergency) \
            if kind in ("", "radio") and channel != NOTES else None
        if w is not None:
            where, args = w
            fm = "t.talkgroup IS NULL" + (f" AND t.channel NOT IN ({','.join('?' * len(not_main))})" if not_main else "")
            county = f"+t.system IN ('', '{sources.COUNTY}')"      # (as view=ours will: never another system's number)
            for cond, extra in [(fm, list(not_main))] + [(f"t.talkgroup = ? AND {county}", [tg]) for tg in main_tgs]:
                main += self._fetch(where + [cond], args + extra, main_limit)
            # +t.talkgroup: no index on it, so the newest-first walk stays on the start (or channel) index; never NULL
            # inside the NOT (NULL IN (...) is NULL, and NOT NULL would drop an airband row from both sections)
            mine = "+t.talkgroup IS NULL" + (f" AND +t.channel NOT IN ({','.join('?' * len(not_main))})" if not_main else "")
            if main_tgs:
                mine = (f"({mine}) OR (+t.talkgroup IS NOT NULL AND +t.talkgroup IN ({','.join('?' * len(main_tgs))}) "
                        f"AND {county})")
            other = self._fetch(where + [f"NOT ({mine})"], args + list(not_main) + main_tgs, limit)
        main = sorted(main, key=lambda r: -r["start"])[:main_limit]
        self._decorate(main + other)
        main = sorted(main + notes, key=lambda r: -r["start"])[:main_limit]
        return main, other

    # ---- the Radio page's views (#415): one page of one filter, older pages by a keyset, changes by a cursor --------

    def _ours_branches(self, main_tgs, not_main) -> list[tuple[str, list]]:
        """"Our calls" as index walks (as split()'s Main, #241): the analog channels but the airband, and each talkgroup
        marked Main, the county system's only (a talkgroup number alone is never read: #409)."""
        fm = "t.talkgroup IS NULL" + (f" AND t.channel NOT IN ({','.join('?' * len(not_main))})" if not_main else "")
        return [(fm, list(not_main))] + [("t.talkgroup = ? AND t.system IN ('', ?)", [int(tg), sources.COUNTY])
                                         for tg in main_tgs]

    # ---- the change log (radio_changes): told after the write, never part of it ----------------------------------

    def _open_changes(self) -> None:
        """The change log at the start: made, or made again when it is an older shape. An earlier build's triggers are
        taken off. Failing: no log, no harm."""
        db = self.store.db
        self.store.after_commit, self.store.after_rollback, self.store.touch = self.flush_changes, self._forget_changes, self.touch
        with self.store.lock:
            try:
                for name in OLD_CHANGE_TRIGGERS:
                    db.execute(f"DROP TRIGGER IF EXISTS {name}")
                cols = {r[1] for r in db.execute("PRAGMA table_info(radio_changes)")}
                if cols and not {"seq", "traffic_id", "system", "gone"} <= cols:
                    db.execute("DROP TABLE radio_changes")
                elif cols and "oseq" not in cols:
                    # the shape before oseq: the column added (its entries: NULL, nobody's but a reader's who may see
                    # everything), seq kept as it was so it only ever grows. Once; a start after writes nothing. Its
                    # index is partial (only entries with an oseq): adding it to a full log writes next to nothing.
                    db.execute("ALTER TABLE radio_changes ADD COLUMN oseq INTEGER")
                db.executescript(CHANGES_SCHEMA)            # (nothing is written here: a start leaves the database as it was)
                # A cursor from before this start: told to reload (what ran at the start, and any entry an earlier run
                # couldn't write, are not in the log): it names another run (cursor()). Read only.
                top = db.execute(TOPS).fetchone()
                self.changes_floor, self.changes_floor_o = int(top[0] or 0), int(top[1] or 0)
                self.changes_broken = False
            except Exception as e:  # noqa: BLE001 - the radio log itself is what matters
                self._changes_off(f"could not be made ({type(e).__name__}: {e})")

    def _changes_off(self, why: str) -> None:
        self.changes_broken = True
        self._retry_at = time.monotonic() + CHANGES_RETRY_S
        if not self._said_broken:
            self._said_broken = True
            try:
                self.log(f"CAD: the Radio page's change log is off ({why}); pages ask afresh instead. Nothing of the radio "
                         "log is lost")
            except Exception:  # noqa: BLE001
                pass

    def touch(self, tid, system: str | None = None, gone: bool = False) -> None:
        """A row of the Radio page changed in the transaction now open: remembered, and written to the change log once
        that transaction has committed (flush_changes; dropped if it is rolled back). system: only for a row this
        writer deleted (it can't be read afterwards). Never raises."""
        try:
            self._pending.append((str(tid), system, bool(gone)))
        except Exception:  # noqa: BLE001
            pass

    def touch_reset(self) -> None:
        """Many rows changed at once, or which ones isn't known: every page asks afresh (one entry, and the floor)."""
        self.touch("reset", None)

    def note_change(self, tid, system: str | None = None, gone: bool = False) -> None:
        """touch() for a writer outside the store's tx(): written now, unless a transaction is open (then with it)."""
        self.touch(tid, system, gone)
        try:
            if not self.store.db.in_transaction:
                self.flush_changes()
        except Exception:  # noqa: BLE001
            pass

    def cursor(self, n: int, sees: bool) -> str:
        """A page's change cursor: which sequence it counts in (s: seq, a reader who may see everything; o: oseq), the
        entry, and this run (a cursor from before a start names another and is told to reload)."""
        return f"{'s' if sees else 'o'}{int(n)}-{self._run}"

    def _read_cursor(self, token, sees: bool) -> int | None:
        """The entry of a cursor this reader may be answered from; None: not one (another sequence or run, garbled)."""
        m = CURSOR.fullmatch(str(token or "").strip())
        if m is None or m["kind"] != ("s" if sees else "o") or m["run"] != self._run:
            return None
        return int(m["n"])

    def _forget_changes(self) -> None:
        self._pending, self._quiet = [], False

    def quiet(self) -> None:
        """The transaction now open writes only another P25 system's data (no row of ours, no incident): once
        committed it doesn't move the rev of a reader who may not see that system (rev_for). Never raises."""
        self._quiet = True

    @staticmethod
    def _everyones(tid: str, system) -> bool:
        """A change every reader may know of: ours, a note, a reset (not another P25 system's, nor an unknown one's)."""
        return (tid == "reset" and system is None) or (system is not None and system in sources.OURS_SYSTEMS)

    def flush_changes(self) -> None:
        """The change notes of the transaction just committed, written to the log: its own statements, after the fact.
        Any failure is caught here: the log is marked broken (readers reload) and tried again in CHANGES_RETRY_S.
        Nothing is raised and no write is repeated: what the notes are about is already stored."""
        notes, self._pending = self._pending, []
        quiet, self._quiet = self._quiet, False
        if not notes:
            try:
                tc = self.store.db.total_changes
                if not quiet and tc != self._tc:
                    # a write with no row of the Radio page (an incident, a unit, a setting): a row's event, status or
                    # address may have changed, so every reader's rev moves; nothing written, or another system's own
                    # data alone (quiet): not
                    self.ours_rev += 1
                self._tc = tc
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            db = self.store.db
            with self.store.lock:
                rows = []
                for tid, system, gone in dict.fromkeys(notes):
                    if system is None and tid != "reset" and not tid.startswith("note-"):
                        r = db.execute("SELECT system FROM radio_traffic WHERE id = ?", (tid,)).fetchone()
                        system = r[0] if r is not None else None
                    elif tid.startswith("note-"):
                        system = ""
                    rows.append((tid, system, int(gone)))
                # a change every reader may know of (not another P25 system's alone): rev_for
                if any(self._everyones(tid, system) for tid, system, _ in rows):
                    self.ours_rev += 1
                rows = [(tid, system, gone, int(self._everyones(tid, system))) for tid, system, gone in rows]
                if self.changes_broken and time.monotonic() < self._retry_at:
                    return
                if db.in_transaction:                   # (never from tx(); a caller's own transaction: with it)
                    db.executemany(INSERT_CHANGE, rows)
                else:
                    # Another process holding the write lock: give up at once (CHANGES_BUSY_MS, not the connection's
                    # 5 s): the writer's thread, and the store's lock it holds, are never kept waiting for the log
                    with self._quick(db):
                        db.execute("BEGIN IMMEDIATE")
                        try:
                            db.executemany(INSERT_CHANGE, rows)
                            top, top_o = db.execute(TOPS).fetchone()
                            db.execute("COMMIT")
                        except BaseException:
                            try:
                                db.execute("ROLLBACK")
                            except Exception:  # noqa: BLE001
                                pass
                            raise
                    if self.changes_broken or any(r[0] == "reset" for r in rows):
                        # written again after a gap, or a reset: what was missed can't be told: older cursors reload
                        # (in oseq only for a gap or every reader's reset: another system's is not the others' to know)
                        self.changes_floor = int(top or 0)
                        if self.changes_broken or any(r[0] == "reset" and r[3] for r in rows):
                            self.changes_floor_o = int(top_o or 0)
                        self.changes_broken = self._said_broken = False
        except Exception as e:  # noqa: BLE001 - never the writer's problem
            self._changes_off(f"an entry could not be written ({type(e).__name__}: {e})")
        finally:
            try:
                self._tc = self.store.db.total_changes      # this transaction's (and the log's own) writes: told of
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    @contextmanager
    def _quick(db):
        """The connection's busy timeout held to CHANGES_BUSY_MS for the change log's own statements, then put back."""
        was = db.execute("PRAGMA busy_timeout").fetchone()[0]
        db.execute(f"PRAGMA busy_timeout = {CHANGES_BUSY_MS}")
        try:
            yield
        finally:
            db.execute(f"PRAGMA busy_timeout = {int(was)}")

    def _trim_changes(self, kind: bool | None = None) -> None:
        """The change log held near its newest CHANGES_KEPT entries of each kind, every CHANGES_TRIM_EVERY transmissions
        of that kind logged and after a prune (never at the start: a start writes nothing). Every reader's entries by
        their own oseq, another system's by seq: how much of one is kept, and when it is trimmed, never depends on the
        other's volume. kind: True every reader's, False another system's, None both. Its own statements, after the
        write; a failure is only a longer log."""
        try:
            with self.store.lock:
                db = self.store.db
                if not db.in_transaction:
                    with self._quick(db):
                        cut = None if kind is False else db.execute(
                            "SELECT seq, oseq FROM radio_changes WHERE oseq <= (SELECT MAX(oseq) FROM radio_changes WHERE oseq IS NOT NULL) - ? "
                            "ORDER BY oseq DESC LIMIT 1", (CHANGES_KEPT,)).fetchone()
                        if cut is not None:
                            db.execute("DELETE FROM radio_changes WHERE oseq <= ?", (cut[1],))
                            self._trimmed, self._trimmed_o = max(self._trimmed, int(cut[0])), max(self._trimmed_o, int(cut[1]))
                        cut = None if kind is True else db.execute(
                            "SELECT seq FROM radio_changes WHERE oseq IS NULL ORDER BY seq DESC LIMIT 1 OFFSET ?",
                            (CHANGES_KEPT,)).fetchone()
                        if cut is not None:
                            db.execute("DELETE FROM radio_changes WHERE oseq IS NULL AND seq <= ?", (cut[0],))
                            self._trimmed = max(self._trimmed, int(cut[0]))
                        self._tc = db.total_changes            # the log's own writes: no reader's rev moves for them
        except Exception:  # noqa: BLE001
            pass

    def rev_for(self, sees: bool | None = None) -> str:
        """The radio log's rev as this reader may know it (GET /api/cad/comms's rev and the state's comms_rev: the page
        asks again when it moves). One who may see everything: the log's and the store's own counters. One who may not
        see another P25 system's traffic: a count of the changes they may know of (every writer notes its change, with
        its row's system: flush_changes, whether or not the change log itself can be written), so that system's
        activity never moves it."""
        if self._sees_context() if sees is None else sees:
            return f"{self.rev}.{self.store.rev}"
        return f"o{self.ours_rev}"

    def page(self, main_tgs, not_main=(), *, view: str = "", system: str = "", group: str = "", source=(),
             service: str = "", tier: str = "", dept: str = "", safety: bool = False, mute=(),
             before: tuple[float, str] | None = None, changed: str | None = None, at: float | None = None,
             limit: int = PAGE, unit: str = "", channel: str = "", event: str = "", pcfd: list[str] | None = None,
             since: float | None = None, until: float | None = None, kind: str = "", text: str = "",
             emergency: bool = False, context: bool | None = None) -> dict:
        """One page of the radio log for the Radio page (#415), newest first: {rows, older, newer, next, cursor, reload,
        gone}.

        view: ours | police | ems | transit | air | all ("" = all). system / group / service / tier / dept: the row's
        own (sizeup/sourcekeys.py). source: only these sources (keys). safety: only scene-safety rows. mute: sources left
        out (not with source=): never one of Our calls, and never a row with the emergency button or an alarm. The
        older filters (unit, channel, event, pcfd, since, until, kind, text, emergency) as in query().
        context: whether the reader may see another system's traffic (None: #419's gate, _sees_context). False:
        those rows don't exist for this answer (left out in the SQL itself, before any search of the words).

        before (start, id): the rows older than that one (the keyset: `next` of the page before). at: a page around that
        time (half newer, half older). older / newer: there are more that way.
        changed: only what changed since that cursor (a page's `cursor`): rows new or whose transcript, alternative
        text, links, kept mark or alarm changed (radio_changes: every writer notes its change, Comms.touch), whatever their
        age; gone: the ids that were deleted or no longer match; reload: too many changed, a note changed, or the cursor
        is older than what is kept, from before this start, or not one this reader was given: ask afresh. A reader who
        may not see another system's traffic is given, and judged by, cursors in oseq (every reader's own sequence):
        no answer to any cursor of theirs depends on that system's traffic.

        Every view walks an index: ours one walk per channel kind and Main talkgroup (radio_traffic_tg_start), a service
        radio_traffic_service_start, a source radio_traffic_source_start (a group or a tier: its sources, resolved
        first), a system radio_traffic_system_start, a department radio_traffic_dept_start, safety its partial index;
        changes are looked up by id. Notes tagged with a unit ride with ours and all, as in Main."""
        limit = max(1, min(int(limit), PAGE_MAX))
        words = [w.lower() for w in text.split()][:8]
        main_tgs, not_main = sorted({int(t) for t in main_tgs}), tuple(dict.fromkeys(not_main))
        srcs = list(dict.fromkeys(str(x) for x in source if x))[:SOURCES_MAX]
        muted = [] if srcs else list(dict.fromkeys(str(x) for x in mute if x))[:MUTE_MAX]
        sees = self._sees_context() if context is None else bool(context)
        # a cursor below the floor (this start, a gap) or below an entry trimmed since: told to reload. A reader who may
        # not see another system's traffic: all in oseq, every reader's own entries (that system's activity moves none)
        floor = max(self.changes_floor, self._trimmed) if sees else max(self.changes_floor_o, self._trimmed_o)
        try:
            hi = 0 if self.changes_broken else int(
                (self.store.one("SELECT MAX(seq) AS hi FROM radio_changes" if sees else
                                "SELECT MAX(oseq) AS hi FROM radio_changes WHERE oseq IS NOT NULL") or {}).get("hi") or 0)
        except sqlite3.Error:
            hi = 0
        cursor = hi
        narrowed = bool(srcs or system or group or service or tier or dept or safety or view in SERVICE_VIEWS)
        out = {"rows": [], "older": False, "newer": False, "next": "", "cursor": self.cursor(cursor, sees), "reload": False,
               "gone": []}

        cond, args = [], []
        if not sees:
            cond.append(f"+t.system IN {sources.OURS_SQL}")
            # Nothing is walked for rows this reader can't have: another system's sources are dropped before any SQL,
            # and a system, view or service with no source of ours answers at once, a department is walked along our
            # sources (no index walk over the hidden rows, whose number would show in how long it takes)
            if system and sources.is_context({"system": system}):
                return out
            seen = [r for r in self.sources_seen() if not sources.is_context(r)]
            srcs = [k for k in srcs if not sources.is_context({"system": (sources.parse(k) or ("", 0))[0]})]
            if source and not srcs:
                return out
            svc = service or (view if view in SERVICE_VIEWS else "")
            if svc and not srcs and not (group or tier):
                theirs = [r["source"] for r in seen if r["service"] == svc and (not system or r["system"] == system)]
                if not theirs:
                    return out
                if len(theirs) <= BRANCH_MAX:
                    srcs = theirs
            if dept and not srcs and not (group or tier):
                # a department is a row's own, not a source's: walked along our sources only (radio_traffic_dept_start
                # would walk another system's rows of that department too)
                mine_d = [r["source"] for r in seen if not system or r["system"] == system]
                if not mine_d:
                    return out
                if len(mine_d) <= BRANCH_MAX:
                    srcs = mine_d
        for col, val in (("t.system", system), ("t.service", view if view in SERVICE_VIEWS else ""), ("t.service", service)):
            if val:
                cond.append(f"{col} = ?")
                args.append(val)
        if dept:
            # as radio_traffic_dept_start is declared; walked by source instead for a reader who may not see another
            # system (+: not that index)
            cond.append("t.dept != '' AND t.dept = ?" if sees else "+t.dept != '' AND +t.dept = ?")
            args.append(dept)
        ours = self._ours_branches(main_tgs, not_main)
        ours_sql = "(" + " OR ".join(f"({c})" for c, _ in ours) + ")"
        ours_args = [a for _, extra in ours for a in extra]
        if group or tier:
            # A group and a tier are a source's own: its sources first (one row each, per rev), then one index walk per
            # source. None: nothing can match, and nothing is read.
            mine = [r["source"] for r in (self.sources_seen() if sees else seen)
                    if (not group or r["group"] == group) and (not tier or r["tier"] == tier)
                    and (not srcs or r["source"] in srcs)]
            if not mine:
                return out
            if len(mine) <= BRANCH_MAX:
                srcs = mine
            for col, val in (('t."group"', group), ("t.tier", tier)):
                if val:
                    cond.append(f"+{col} = ?")
                    args.append(val)
        if muted:
            # a muted source's rows are left out, but never one of Our calls, never a fire channel of ours (fire
            # channels can't be muted) and never an emergency (#128)
            keep = ["+t.emergency = 1", ours_sql, f"(+t.service = 'fire' AND +t.system IN {sources.OURS_SQL})"] + (["t.id IN (SELECT traffic_id FROM alarms)"] if has_alarms(self.store) else [])
            cond.append(f"(+t.source NOT IN ({','.join('?' * len(muted))}) OR " + " OR ".join(keep) + ")")
            args += muted + ours_args
        hint = ""
        if safety:
            # few rows: from the safety index, the view and the sources as plain conditions on them
            hint = " INDEXED BY radio_traffic_safety"
            cond.append(SAFETY_WHERE)
            if srcs:
                cond.append(f"+t.source IN ({','.join('?' * len(srcs))})")
                args += srcs
            if view == "ours":
                cond.append(ours_sql.replace("t.talkgroup", "+t.talkgroup"))
                args += ours_args
            branches = [("1", [])]
        elif srcs:
            branches = [("t.source = ?", [k]) for k in srcs]
            if view == "ours":
                cond.append(ours_sql)
                args += ours_args
        elif view == "ours":
            branches = ours
        else:
            branches = [("1", [])]

        w = self._radio_where(unit, channel, event, pcfd, since, until, words, emergency) \
            if kind in ("", "radio") and channel != NOTES else None
        notes_ok = kind in ("", "notes") and channel in ("", NOTES) and not emergency and not narrowed
        radio: list[dict] = []
        if changed is not None:
            changed = self._read_cursor(changed, sees)
            if changed is None or self.changes_broken or changed > hi or changed < floor:
                # not a cursor this reader was given in this run (another sequence, another start, another database),
                # no log now, a gap, or older than is kept
                return {**out, "reload": True}
            # Another system's changes are not this reader's to know of: not as rows, not as `gone`, not in the count
            # (nor an id with any entry of another system's, or of none: radio_changes_id)
            ours = sources.OURS_SQL
            key = "c.seq" if sees else "c.oseq"
            mine = "" if sees else (
                f" AND NOT EXISTS (SELECT 1 FROM radio_changes x WHERE x.traffic_id = c.traffic_id "
                f"AND x.traffic_id != 'reset' AND x.oseq IS NULL) AND NOT EXISTS (SELECT 1 FROM radio_traffic t "
                f"WHERE t.id = c.traffic_id AND t.system NOT IN {ours})")
            ids = [r["traffic_id"] for r in self.store.q(
                f"SELECT DISTINCT c.traffic_id FROM radio_changes c WHERE {key} > ? AND {key} <= ?{mine} LIMIT ?",
                (changed, hi, CHANGES_MAX + 1))]
            if "reset" in ids:
                return {**out, "reload": True}
            if len(ids) > CHANGES_MAX or (notes_ok and any(i.startswith("note-") for i in ids)):
                return {**out, "reload": True}
            ids = [i for i in ids if not i.startswith("note-")]
            if ids and w is not None:
                any_branch = "(" + " OR ".join(f"({c})" for c, _ in branches) + ")"
                radio = self.store.q(
                    # CROSS JOIN: the ids first, each row by its key, whatever index the view's own conditions offer
                    "SELECT t.*, 'radio' AS kind FROM json_each(?) j CROSS JOIN radio_traffic t ON t.id = j.value WHERE "
                    + " AND ".join(f"({x})" for x in w[0] + cond + [any_branch]),
                    (json.dumps(ids), *w[1], *args, *[a for _, extra in branches for a in extra]))
            have = {r["id"] for r in radio}
            out["gone"] = [i for i in ids if i not in have]      # deleted, or no longer what was asked: off the page
        elif w is not None:
            where, wargs = w[0] + cond, w[1] + args
            if at is not None:
                half = max(1, limit // 2)
                new = self._walk(branches, where + ["t.start >= ?"], wargs + [float(at)], half + 1, asc=True, hint=hint)
                old = self._walk(branches, where + ["t.start < ?"], wargs + [float(at)], limit - half + 1, hint=hint)
                out["newer"], out["older"] = len(new) > half, len(old) > limit - half
                radio = new[:half] + old[:limit - half]
            else:
                if before is not None:
                    where = where + ["(t.start < ? OR (t.start = ? AND t.id < ?))"]
                    wargs = wargs + [float(before[0]), float(before[0]), str(before[1])]
                radio = self._walk(branches, where, wargs, limit + 1, hint=hint)
                out["older"] = len(radio) > limit
                radio = radio[:limit]
        notes: list[dict] = []
        if notes_ok and changed is None:
            lo, hi = since, until
            if at is not None and radio:
                lo, hi = max(lo or 0.0, min(r["start"] for r in radio)), min(hi or 1e18, max(r["start"] for r in radio))
            elif before is not None:
                hi = min(hi or 1e18, float(before[0]) - 1e-6)
            notes = self._notes(unit, event, pcfd, lo, hi, limit + 1, words)
            if at is None:
                out["older"] = out["older"] or len(notes) > limit
                notes = notes[:limit]
        self._decorate(radio)
        mains = set(main_tgs)
        for r in radio:
            r["main"] = (r["channel"] not in not_main if r["talkgroup"] is None
                         else r["talkgroup"] in mains and r["system"] in ("", sources.COUNTY))
        for r in notes:
            r["main"] = True
        rows = sorted(sorted(radio + notes, key=lambda r: r["id"], reverse=True), key=lambda r: -r["start"])
        if changed is None and at is None and len(rows) > limit:
            rows, out["older"] = rows[:limit], True
        out["rows"] = rows
        if rows:
            last = rows[-1]
            out["next"] = f"{last['start']!r},{last['id']}"
        return out

    def _walk(self, branches, where, args, limit: int, asc: bool = False, hint: str = "") -> list[dict]:
        """The newest (asc: the oldest) `limit` rows over these branches (one index walk each), in that order."""
        way = "ASC" if asc else "DESC"
        got: list[dict] = []
        for c, extra in branches:
            got += self.store.q(f"SELECT t.*, 'radio' AS kind FROM radio_traffic t{hint} WHERE "
                                + " AND ".join(f"({x})" for x in where + [c])
                                + f" ORDER BY t.start {way}, t.id {way} LIMIT ?", (*args, *extra, limit))
        if len(branches) > 1:
            seen: dict[str, dict] = {}
            for r in got:
                seen.setdefault(r["id"], r)
            got = sorted(seen.values(), key=lambda r: (r["start"], r["id"]), reverse=not asc)[:limit]
        return got

    def sources_seen(self) -> list[dict]:
        """Every source in the log or the activity table: [{source, channel, system, group, service, tier, talkgroup,
        last}] (last: its newest transmission or active minute, unix). A skip along radio_traffic_source_start and the
        activity table's key: one look-up per source, not a scan. Once per rev."""
        return self._per_rev("sources_seen", self._sources_seen)

    def _sources_seen(self) -> list[dict]:
        skip = ("WITH RECURSIVE g(k) AS (SELECT MIN(source) FROM {t} WHERE source > '' UNION ALL "
                "SELECT (SELECT MIN(source) FROM {t} WHERE source > g.k) FROM g WHERE g.k IS NOT NULL) "
                "SELECT k FROM g WHERE k IS NOT NULL")
        keys = dict.fromkeys(r["k"] for t in ("radio_traffic", "radio_activity") for r in self.store.q(skip.format(t=t)))
        out = []
        for k in keys:
            row = self.store.one('SELECT channel, system, "group" AS grp, service, tier, talkgroup, start FROM radio_traffic '
                                 "WHERE source = ? ORDER BY start DESC LIMIT 1", (k,)) or {}
            act = self.store.one("SELECT MAX(minute) AS m FROM radio_activity WHERE source = ?", (k,)) or {}
            last = max(float(row.get("start") or 0.0), float(act.get("m") or 0) * 60.0)
            out.append({"source": k, "channel": row.get("channel") or "", "system": row.get("system") or "",
                        "group": row.get("grp") or "", "service": row.get("service") or "", "tier": row.get("tier") or "",
                        "talkgroup": row.get("talkgroup"), "last": last or None})
        return out

    def minute_counts(self, now: float, minutes: int = COUNT_MINUTES) -> dict[str, list[list[int]]]:
        """{source: [[minutes ago, calls], ...]} over the last `minutes` (0: the minute of `now`), only the minutes with
        calls, newest first: from the activity table (radio_activity_minute). The sources' unread counts and the time
        bar (#415)."""
        end = int(now // 60)
        out: dict[str, list[list[int]]] = {}
        for r in self.store.q("SELECT source, minute, calls FROM radio_activity WHERE minute > ? AND minute <= ? "
                              "ORDER BY minute DESC", (end - minutes, end)):
            out.setdefault(r["source"], []).append([end - r["minute"], int(r["calls"])])
        return out

    def emergencies(self, now: float, window: float = EM_WINDOW_S) -> dict[str, dict]:
        """{source: {emergency: "ours" | "other", stuck}} for the sources with an alarm of ours still open: an alarm
        or a notice the CAD raised on a transmission in the last `window` (sizeup/cad/alarms.py) that nobody has
        cleared. ours: an alarm (alarm_level alarm); other: a notice (someone else's emergency button, #128; stuck: the
        radio its flag is stuck on, #190). Seen (acknowledged) but not cleared: still open, still marked.
        Only what has an alarm row, so every mark can be cleared (All clear); a bare emergency flag with no alarm, and
        any row of another P25 system (context: it never raises an alarm), marks nothing."""
        if not has_alarms(self.store):
            return {}
        rows = self.store.q("SELECT t.* FROM radio_traffic t WHERE t.id IN (SELECT traffic_id FROM alarms "
                            "WHERE at >= ? ORDER BY at DESC LIMIT 200)", (now - window,))
        with_alarms(self.store, rows)
        out: dict[str, dict] = {}
        for r in rows:
            if not r["alarm_id"] or r["clear_at"] or sources.is_context(r):
                continue
            e = out.setdefault(r["source"], {"emergency": "other", "stuck": ""})
            if r["alarm_level"] == "alarm":
                e["emergency"] = "ours"
            e["stuck"] = e["stuck"] or r["alarm_stuck"] or ""
        return out

    def source_names(self, context: bool = True) -> list[str]:
        """The channels in the log, newest heard first; context False: not another system's."""
        rows = [r for r in self.sources_seen() if context or not sources.is_context(r)]
        return [r["channel"] for r in sorted(rows, key=lambda r: -(r["last"] or 0)) if r["channel"]]

    def _radio(self, unit, channel, event, pcfd, since, until, limit, words=(), emergency=False) -> list[dict]:
        w = self._radio_where(unit, channel, event, pcfd, since, until, words, emergency)
        if w is None:
            return []
        rows = self._fetch(*w, limit)
        self._decorate(rows)
        return rows

    def _radio_where(self, unit, channel, event, pcfd, since, until, words=(), emergency=False):
        """The Radio view's filters as SQL (where, args); None: nothing can match (PCFD only, no PCFD units)."""
        where, args = [], []
        if emergency:
            where.append(EMERGENCY_WHERE if has_alarms(self.store) else "t.emergency = 1")
        for w in words:
            where.append("LOWER(t.text) LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(w)}%")
        if unit:
            self._rig_where(where, args, [unit])
        if channel:
            where.append("t.channel = ?")
            args.append(channel)
        if event:
            where.append("t.id IN (SELECT l.traffic_id FROM radio_links l JOIN incidents i ON i.id = l.incident_id "
                         "WHERE i.number = ? OR CAST(i.id AS TEXT) = ?)")
            args += [event, event]
        if pcfd is not None:
            if not pcfd:
                return None
            self._rig_where(where, args, pcfd)
        if since is not None:
            where.append("t.start >= ?")
            args.append(since)
        if until is not None:
            where.append("t.start <= ?")
            args.append(until)
        return where, args

    def _rig_where(self, where: list, args: list, units) -> None:
        """Rows whose rig names any of these units: by their rowids when few (RIG_ROWS_MAX), else by the rig itself."""
        pats = [f"%,{u},%" for u in units]
        rows = self.store.q(rig_rows_sql(len(pats)), pats)
        if len(rows) <= RIG_ROWS_MAX:
            where.append("t.rowid IN (SELECT value FROM json_each(?))")
            args.append(json.dumps([r["r"] for r in rows]))
        else:
            where.append("(" + " OR ".join([RIG_LIKE.format(col="t.rig")] * len(pats)) + ")")
            args += pats

    def _sees_context(self) -> bool:
        return sources.may_see_context(self.context_broadcast)

    def _fetch(self, where, args, limit) -> list[dict]:
        if not self._sees_context():
            where = list(where) + [f"+t.system IN {sources.OURS_SQL}"]     # (+: the walk stays on its index)
        return self.store.q("SELECT t.*, 'radio' AS kind FROM radio_traffic t"
                            + (" WHERE " + " AND ".join(f"({w})" for w in where) if where else "")
                            + " ORDER BY t.start DESC LIMIT ?", (*args, limit))

    def _decorate(self, rows) -> None:
        """Each row's events, its page kind and its alarm (in place)."""
        if rows:
            ev = self.events_of([r["id"] for r in rows])
            for r in rows:
                r["events"] = ev.get(r["id"], [])
                _tones(r, self.paging, self.names_ours)
            with_alarms(self.store, rows)
            if self.atc is not None:
                self.atc(rows)
            if self.fix is not None:
                self.fix(rows)                  # status words put back after our callsigns (statusrepair.py)

    def _per_rev(self, name: str, make):
        """make() once per rev (a new or changed transmission): every filter of the Radio view asks for these (#284)."""
        hit = self._revs.get(name)
        if hit is None or hit[0] != self.rev:
            hit = self._revs[name] = (self.rev, make())
        return hit[1]

    def talkgroups(self) -> list[int]:
        """Every talkgroup logged, by a skip along radio_traffic_tg_start (one look-up each, not a scan)."""
        return self._per_rev("talkgroups", self._talkgroups)

    def _talkgroups(self) -> list[int]:
        return [r["tg"] for r in self.store.q(
            "WITH RECURSIVE g(tg) AS (SELECT MIN(talkgroup) FROM radio_traffic UNION ALL "
            "SELECT (SELECT MIN(talkgroup) FROM radio_traffic WHERE talkgroup > g.tg) FROM g WHERE g.tg IS NOT NULL) "
            "SELECT tg FROM g WHERE tg IS NOT NULL")]

    def facets(self) -> dict:
        """The Radio view's choices: every channel, latest heard first, and every rig. Each by a skip along an index
        (radio_traffic_channel_start, radio_traffic_rig): one look-up per value, not a scan of the log (#284)."""
        return self._per_rev("facets", self._facets)

    def _facets(self) -> dict:
        chans, rigs = self.store.q(CHANNELS_SQL), self.store.q(RIGS_SQL)
        return {"channels": [r["channel"] for r in chans] + [NOTES],
                "rigs": sorted({x.strip() for r in rigs for x in r["rig"].split(",") if x.strip()})}


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
    rig_ours = getattr(store, "rig_ours", None)                # set by Alarms (sizeup/cad/alarms.py)
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
        # one of our rigs keyed it (the radio registry): its emergency stands out on any talkgroup (cad.js emLoud),
        # also a repeat press that has no alarm row of its own
        r["em_ours"] = bool((r["emergency"] or r.get("kept")) and rig_ours is not None and r.get("rig") and rig_ours(r["rig"]))
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
    if why == "mention":
        return f"names its {unit}" if unit in ("address", "place") else f"names {unit or 'a unit on it'}"
    if why == "fireground":
        return f"its fireground channel {unit}".rstrip()
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
    bare = r["page"] in ("unknown", "none")       # a list of bare numbers counts only then (feed.Matcher.units_named)
    r["names_ours"] = (names_ours(r.get("text") or "", bare) if names_ours and r["page"] in ("other", "unknown", "none")
                       else [])
    return r


def _when(t) -> str:
    return datetime.fromtimestamp(t["start"], TZ).strftime("%H:%M:%S")


def _who(t) -> str:
    return t["rig"] or t["talker"] or t["radio_id"] or "radio"


def _row_line(t) -> str:
    """A transmission as an event's timeline names it: "14:02:11 E63 on FG1". Another P25 system's (#419): when and
    on which channel only, never who (its talker, its radio ID) nor what was said."""
    return f"{_when(t)} on {t['channel']}" if sources.is_context(t) else f"{_when(t)} {_who(t)} on {t['channel']}"


def _like(w: str) -> str:
    """A word for LIKE: its own % and _ (and the escape) taken literally."""
    return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
