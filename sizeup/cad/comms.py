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
import re
import time
from datetime import datetime

from sizeup import sourcekeys as sources
from sizeup.cad import airport
from sizeup.cad.radios import is_dispatcher
from sizeup.cad.store import TZ

TURN = re.compile(r"^(?P<call>.+)-t(?P<k>\d+)$")
NOTES = "Notes"          # the /comms "channel" of event notes tagged with a unit
ACTIVITY_RADIOS_S = 7 * 24 * 3600    # radio_activity: which radios keyed is kept this long (the counts longer)
KEEP_UNLINKED_S = 90 * 24 * 3600     # transmissions on no event: kept 90 days (linked ones: always)
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
EM_WINDOW_S = 12 * 3600               # the sources' emergency mark: unacknowledged ones this recent
# The viewer may not see another system's traffic (context, #419's gate): only these systems' rows exist for them
FIRE_SYSTEMS_SQL = "+t.system IN ('', ?, 'analog', 'airband')"
# Every change to what a row of the Radio page shows, in order (#415 ?changed=): the transmission itself (new, its
# transcript, a re-run, the alternative text, pruned), its links to incidents, a link kept, its alarm (raised, seen,
# cleared), and a note tagged with a unit ('note-<id>'). Written by triggers, so no writer can forget; seq only grows
# and SQLite has one writer at a time, so a reader that has seen seq N has seen everything up to N.
CHANGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS radio_changes (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  traffic_id TEXT NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0       -- 1: the transmission was deleted
);
CREATE TRIGGER IF NOT EXISTS radio_changes_t_ins AFTER INSERT ON radio_traffic BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (NEW.id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_t_upd AFTER UPDATE ON radio_traffic BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (NEW.id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_t_del AFTER DELETE ON radio_traffic BEGIN
  INSERT INTO radio_changes (traffic_id, gone) VALUES (OLD.id, 1); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_l_ins AFTER INSERT ON radio_links BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (NEW.traffic_id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_l_upd AFTER UPDATE ON radio_links BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (NEW.traffic_id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_l_del AFTER DELETE ON radio_links BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (OLD.traffic_id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_k_ins AFTER INSERT ON radio_link_kept BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (NEW.traffic_id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_k_del AFTER DELETE ON radio_link_kept BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (OLD.traffic_id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_n_ins AFTER INSERT ON events WHEN NEW.kind = 'note' AND NEW.units != '' BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES ('note-' || NEW.id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_n_upd AFTER UPDATE ON events WHEN NEW.kind = 'note' BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES ('note-' || NEW.id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_n_del AFTER DELETE ON events WHEN OLD.kind = 'note' BEGIN
  INSERT INTO radio_changes (traffic_id, gone) VALUES ('note-' || OLD.id, 1); END;
"""
ALARM_CHANGES_SCHEMA = """
CREATE TRIGGER IF NOT EXISTS radio_changes_a_ins AFTER INSERT ON alarms BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (NEW.traffic_id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_a_upd AFTER UPDATE ON alarms BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (NEW.traffic_id); END;
CREATE TRIGGER IF NOT EXISTS radio_changes_a_del AFTER DELETE ON alarms BEGIN
  INSERT INTO radio_changes (traffic_id) VALUES (OLD.traffic_id); END;
"""
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
        # The airband rows' ATC talker (sizeup/atc_talkers.py AtcTalkers.decorate, #252): set by the CAD; rows in place
        self.atc = None
        self.fix = None                 # rows -> rows: the status hints' decorate (sizeup/cad/statushints.py)
        self.names_ours = None  # text -> [our unit codes] (the feed's matcher, #115): a page not ours naming them
        self.on_text = None     # traffic id -> None: a transcript arrived or changed (status suggestions, #37)
        self._revs: dict = {}   # name -> (rev, value): what _per_rev made
        self._alarm_triggers = False     # the alarms' changes go to radio_changes too (_alarm_changes)
        with self.store.lock:
            self.store.db.executescript(KEPT_SCHEMA)
            self.store.db.executescript(CHANGES_SCHEMA)
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
            mhz = t.get("mhz")                  # the frequency heard on (an airband row, #260); kept when not sent again
            mhz = round(float(mhz), 4) if isinstance(mhz, (int, float)) and not isinstance(mhz, bool) and mhz > 0 else None
            # its source, by its system (#409 / #415: sizeup/sourcekeys.py); another P25 system's is context only
            system = str(t.get("system") or "")
            other = bool(system) and p25 and system != sources.COUNTY
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
            if other:
                # Another system's units never put traffic on our incidents by a rule (#415): no unit, airport or
                # dispatcher link. Only a person's own link does (link()).
                self.rev += 1
                return sorted(r[0] for r in db.execute("SELECT incident_id FROM radio_links WHERE traffic_id = ?", (tid,)))
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
                              + ("talkgroup IS NULL" if fm else "talkgroup IS NOT NULL AND system IN ('', ?)")
                              + " AND (', ' || radio_id || ', ') LIKE ? ORDER BY start DESC LIMIT ?",
                              (*([] if fm else [sources.COUNTY]), f"%, {rid}, %", RELINK_MAX)).fetchall()
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
            db.execute("DELETE FROM radio_link_kept WHERE traffic_id = ? AND incident_id = ?", (tid, iid))
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
            db.execute("DELETE FROM radio_activity WHERE minute < ?", (int(cutoff // 60),))
            db.execute("DELETE FROM radio_changes WHERE seq <= (SELECT MAX(seq) FROM radio_changes) - ?", (CHANGES_KEPT,))
            db.execute("UPDATE radio_activity SET radios = '' WHERE radios != '' AND minute < ?",
                       (int((self.clock() - ACTIVITY_RADIOS_S) // 60),))
            return db.execute(f"DELETE FROM radio_traffic WHERE start < ? AND id NOT IN (SELECT l.traffic_id FROM radio_links l "
                              f"WHERE {SURE_LINK})", (cutoff,)).rowcount

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
              kind: str = "", text: str = "", emergency: bool = False, context: bool = True) -> tuple[list[dict], list[dict]]:
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
        w = self._radio_where(unit, channel, event, pcfd, since, until, words, emergency, context) \
            if kind in ("", "radio") and channel != NOTES else None
        if w is not None:
            where, args = w
            fm = "t.talkgroup IS NULL" + (f" AND t.channel NOT IN ({','.join('?' * len(not_main))})" if not_main else "")
            for cond, extra in [(fm, list(not_main))] + [("t.talkgroup = ?", [tg]) for tg in main_tgs]:
                main += self._fetch(where + [cond], args + extra, main_limit)
            # +t.talkgroup: no index on it, so the newest-first walk stays on the start (or channel) index; never NULL
            # inside the NOT (NULL IN (...) is NULL, and NOT NULL would drop an airband row from both sections)
            mine = "+t.talkgroup IS NULL" + (f" AND +t.channel NOT IN ({','.join('?' * len(not_main))})" if not_main else "")
            if main_tgs:
                mine = f"({mine}) OR (+t.talkgroup IS NOT NULL AND +t.talkgroup IN ({','.join('?' * len(main_tgs))}))"
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

    def _alarm_changes(self) -> None:
        """The alarms' changes into radio_changes too, once the alarms table is there (it is made after this)."""
        if not self._alarm_triggers and has_alarms(self.store):
            with self.store.lock:
                self.store.db.executescript(ALARM_CHANGES_SCHEMA)
            self._alarm_triggers = True

    def page(self, main_tgs, not_main=(), *, view: str = "", system: str = "", group: str = "", source=(),
             service: str = "", tier: str = "", dept: str = "", safety: bool = False, mute=(),
             before: tuple[float, str] | None = None, changed: int | None = None, at: float | None = None,
             limit: int = PAGE, unit: str = "", channel: str = "", event: str = "", pcfd: list[str] | None = None,
             since: float | None = None, until: float | None = None, kind: str = "", text: str = "",
             emergency: bool = False, context: bool = True) -> dict:
        """One page of the radio log for the Radio page (#415), newest first: {rows, older, newer, next, cursor, reload,
        gone}.

        view: ours | police | ems | transit | air | all ("" = all). system / group / service / tier / dept: the row's
        own (sizeup/sourcekeys.py). source: only these sources (keys). safety: only scene-safety rows. mute: sources left
        out (not with source=): never one of Our calls, and never a row with the emergency button or an alarm. The
        older filters (unit, channel, event, pcfd, since, until, kind, text, emergency) as in query().
        context False: the viewer may not see another system's traffic: those rows don't exist for this answer (left
        out in the SQL itself, before any search of the words).

        before (start, id): the rows older than that one (the keyset: `next` of the page before). at: a page around that
        time (half newer, half older). older / newer: there are more that way.
        changed: only what changed since that cursor (a page's `cursor`): rows new or whose transcript, alternative
        text, links, kept mark or alarm changed (radio_changes: every writer, by triggers), whatever their age; gone:
        the ids that were deleted or no longer match; reload: too many changed, a note changed, or the cursor is older
        than what is kept: ask afresh.

        Every view walks an index: ours one walk per channel kind and Main talkgroup (radio_traffic_tg_start), a service
        radio_traffic_service_start, a source radio_traffic_source_start (a group or a tier: its sources, resolved
        first), a system radio_traffic_system_start, a department radio_traffic_dept_start, safety its partial index;
        changes are looked up by id. Notes tagged with a unit ride with ours and all, as in Main."""
        limit = max(1, min(int(limit), PAGE_MAX))
        words = [w.lower() for w in text.split()][:8]
        main_tgs, not_main = sorted({int(t) for t in main_tgs}), tuple(dict.fromkeys(not_main))
        srcs = list(dict.fromkeys(str(x) for x in source if x))[:SOURCES_MAX]
        muted = [] if srcs else list(dict.fromkeys(str(x) for x in mute if x))[:MUTE_MAX]
        self._alarm_changes()
        ends = self.store.one("SELECT MIN(seq) AS lo, MAX(seq) AS hi FROM radio_changes") or {}
        cursor = int(ends.get("hi") or 0)
        narrowed = bool(srcs or system or group or service or tier or dept or safety or view in SERVICE_VIEWS)
        out = {"rows": [], "older": False, "newer": False, "next": "", "cursor": cursor, "reload": False, "gone": []}

        cond, args = [], []
        if not context:
            cond.append(FIRE_SYSTEMS_SQL)
            args.append(sources.COUNTY)
        for col, val in (("t.system", system), ("t.service", view if view in SERVICE_VIEWS else ""), ("t.service", service)):
            if val:
                cond.append(f"{col} = ?")
                args.append(val)
        if dept:
            cond.append("t.dept != '' AND t.dept = ?")           # as radio_traffic_dept_start is declared
            args.append(dept)
        ours = self._ours_branches(main_tgs, not_main)
        ours_sql = "(" + " OR ".join(f"({c})" for c, _ in ours) + ")"
        ours_args = [a for _, extra in ours for a in extra]
        if group or tier:
            # A group and a tier are a source's own: its sources first (one row each, per rev), then one index walk per
            # source. None: nothing can match, and nothing is read.
            mine = [r["source"] for r in self.sources_seen()
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
            # a muted source's rows are left out, but never one of Our calls and never an emergency (#128)
            keep = ["+t.emergency = 1", ours_sql] + (["t.id IN (SELECT traffic_id FROM alarms)"] if has_alarms(self.store) else [])
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
            changed = int(changed)
            lo = int(ends.get("lo") or 0)
            if changed > cursor or (lo and changed < lo - 1):
                return {**out, "reload": True}                   # a cursor from another database, or older than is kept
            ids = [r["traffic_id"] for r in self.store.q(
                "SELECT DISTINCT traffic_id FROM radio_changes WHERE seq > ? AND seq <= ? LIMIT ?",
                (changed, cursor, CHANGES_MAX + 1))]
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
        """{source: {emergency: "ours" | "other", stuck}} for the sources with an emergency nobody has seen yet (the
        emergency button or an alarm on a transmission in the last `window`, not acknowledged, not cleared): ours when
        it raised an alarm (alarm_level alarm), else someone else's (#128, #190: stuck: the radio its flag is stuck on).
        From radio_traffic_emergency and the alarms table: a handful of rows."""
        since = now - window
        rows = self.store.q("SELECT t.* FROM radio_traffic t INDEXED BY radio_traffic_emergency WHERE t.emergency = 1 "
                            "AND t.start >= ? ORDER BY t.start DESC LIMIT 200", (since,))
        if has_alarms(self.store):
            have = {r["id"] for r in rows}
            rows += [r for r in self.store.q("SELECT t.* FROM radio_traffic t WHERE t.id IN (SELECT traffic_id FROM alarms "
                                             "WHERE at >= ? ORDER BY at DESC LIMIT 200)", (since,)) if r["id"] not in have]
        with_alarms(self.store, rows)
        out: dict[str, dict] = {}
        for r in rows:
            if not r["emergency"] and not r["alarm_id"]:
                continue
            if r["ack_at"] or r["clear_at"]:
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

    def _radio_where(self, unit, channel, event, pcfd, since, until, words=(), emergency=False, context=True):
        """The Radio view's filters as SQL (where, args); None: nothing can match (PCFD only, no PCFD units).
        context False: another system's rows are left out, first (before the words are searched)."""
        where, args = [], []
        if not context:
            where.append(FIRE_SYSTEMS_SQL)
            args.append(sources.COUNTY)
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

    def _fetch(self, where, args, limit) -> list[dict]:
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
