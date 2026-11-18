"""The intel engine: radio transcripts in, pre-alert events out (sizeup/intel).

    IntelEngine(*, packs=None, settings=None, events=None, sink=None, locate=None, clock=time.time, thread=True,
                store=None, log=print, tz=None)

        packs      compiled packs (rules.load_packs); None: data/intel/*.json with the settings' overrides
        settings   a snapshot {"intel": {...}, "intel_packs": {...}, "intel_rules": {...}}, or a function returning one
                   (live: read from the CAD's Settings at every reload())
        events     the event store: None = cad.sqlite (events.SqlEvents(store)); replay passes events.MemoryEvents()
        sink       where the effects go: None with a store = LiveSink (intel_hits, the log, ntfy); replay: a recorder
        locate     locate(text, scope) -> model.Place | None, read only (place.Locator over the gazetteer)
        thread     True: consider(tid) is queued for the "intel" thread (the live default, started by start());
                   False: no thread, no leader or fence checks: feed() and tick() are called directly (replay, tests)

    engine.feed(row)      one radio_traffic row {id (str), start, end, source, system, service, dept, tier, talkgroup,
                          channel, talker, rig, radio_id, text, alt_text, model}: synchronous
    engine.consider(tid)  live: queue the id (never waits on rules or the gazetteer); the thread loads the row, feed()s it
    engine.tick(now)      expires events past their until, sends a due digest, forgets old windows
    engine.ruleset_hash()
    engine.counts(now)    what it did with what it was handed, per pack (sizeup/intel/counters.py): Settings > System >
                          Intel and Settings > Station > Health; one "intel: considered N, ..." log line an hour
    engine.push_sources() (frozenset of source keys, revision): the sources intel could push from right now, for
                          speech-to-text's priority (see "Push sources" below); sizeup.intel.push_sources() without
                          the engine at hand
    engine.is_push_source(channel_key, kind="")   that check for one speech-to-text channel key ("tg:307", "FG1"):
                          sizeup.intel.is_push_source() without the engine

Push sources (the hook for speech-to-text's queue: a source intel can push from is transcribed at HIGH or above).
The engine runs on the HUB (the CAD of record; all-in-one too). A station's engine is idle (active() is False while it
forwards to a hub), so there sizeup.intel.push_sources() is (frozenset(), 0): a station that wants the set gets it
from the hub over whatever hub-to-station channel speech-to-text uses (nothing here sends it).

    keys  qualified source keys (sizeup/sourcekeys.py): p25:<system>:<tg> (the county's p25:westchester:<n>),
          fm:<label>, air:<label>. A source is in it when intel is on (the "enabled" switch), pushes are on (the kill
          switch "push"), the INTEL kind has a topic in effect (LiveSink pushes nothing without one), and a pack that
          could push reads it: the pack's mode is push and one of its rules is on, at push, in a category at push
          (Settings' intel_packs / intel_rules applied; can_push()). Which sources a pack reads is Pack.in_scope() itself,
          the predicate feed() uses, over every known source as a row of the radio log would carry it (source_rows():
          the talkgroups files' talkgroups with their service as tagged live, the admins' radio_services choices over
          it, and the configured FM and airband channels). So the set never disagrees with what the engine reads.
          Quiet hours and the rate caps are left out: they hold a push for a while, the source stays one that pushes.
    rev   a content hash of the set (push_rev(): the first 8 bytes of sha256 over the sorted keys, one per line,
          masked to 53 bits so JSON and JavaScript keep it exact; never 0). The same set always has the same rev, in
          any process and after a restart, so a hub that restarts can never hand a station a rev it already holds with
          other keys; a different set has another one. The empty set has its own (non-zero) rev; 0 is only "no engine
          here / not computed" (sizeup.intel.push_sources() on a station). The set is recomputed after reload() (the
          packs, intel's Settings) and sources_changed() (any Settings change: radio_services, the notify topics), and
          at most PUSH_REFRESH_S after the last (a station's talkgroups list arriving at the hub); otherwise cached.

    is_push_source(channel_key, kind="") -> bool: the check speech-to-text should make (sizeup.intel.is_push_source()
    without the engine), never a raw `key in keys`. STT keys a transmission by its channel key (sourcekeys.channel_key,
    app.traffic_key): "tg:<n>" for the county's talkgroup, "p25:<system>:<n>" for another system's, and the BARE label
    for an FM or airband channel ("FG1", "HPN Tower"). The set holds source keys as rows carry them, so the key is
    normalised the way rows are keyed (source_key()): a talkgroup by sourcekeys.service_key ("tg:307" is
    "p25:westchester:307"); a bare label by sourcekeys.fm(label, kind) as App._row_source does it (kind "airband":
    "air:<label>", any other kind: "fm:<label>"; kind not given: either one, a channel's label being unique here); a
    key already "fm:" / "air:" as it is. A caller that compares itself must normalise the same way (source_key()).

The sink (no side effect happens in the engine itself):

    sink.hit(Hit)                 every hit: shadow, below threshold and suppressed-by-mode too
    sink.event(Event, change)     raised | updated | ended | expired
    sink.push(Event, Decision)    the policy's verdict; Decision.message is the composed push when would_push (scrubbed
                                  only with push_pii off)
    sink.clear(tid, keep)         optional: a re-transcription, the rules that no longer hit tid
    sink.digest(events)           optional: the events held over a rate cap, when a digest is due

How a row becomes an event, per pack whose scope it is in:
  1. its rules over the text (and the other engine's alt_text, at the pack's context_factor); a "suppress" phrase
     ("disregard", "training") gives it no hits and ends the source's open events from this window;
  2. the row joins its source's window (window_s): the dispatcher's call-out and the unit's read-back are one candidate.
     Idempotent per traffic id: the row's hits replace what it had before;
  3. per category, WHAT = noisy-OR of each rule's best weight in the window (a "*" cue only beside a real hit), times
     (1 + w) for each damper (negative weight); at or over the threshold (the category's, else the pack's) it is an
     event: the open one of this pack, category and source still within the window, or the open one at the same place
     (subject = pack:category:place), else a new one;
  4. WHERE: the window's words through locate() (only while not confirmed);
  5. mode = the lowest of the pack's, the category's and the rules' rung (off: no event; shadow: an event only in the
     store, never shown or pushed); policy.decide() says whether to push, push.compose() (+ scrub() with push_pii
     off) what. The rules'
     rung is the highest mode M such that the rules at M or above ALONE (their weights, every cue whatever its mode,
     every damper) reach the threshold: a rule at a lower mode adds to WHAT but never lifts the event over its mode, so
     raising one weak rule to push never makes the strong ones beside it push (#535 review). With every rule of a
     category at one mode (as shipped) this is that mode, as before.
  6. a pack with an area (rules.Pack.area: the county-wide packs; never the police pack) holds an event at log,
     whatever the switches say (Event.suppressed says why), unless it was already pushed (never lowered then):
       out_of_area    its place is in another town, or, with no place, its words say it is in towns and none of the
                      area's (towns_said: after "in / at / on", never "to / from / units from"); a safety category
                      with no place never is;
       near_incident  its place is one of our own accepted emergency incidents' (the same label, or within
                      NEAR_INCIDENT_M) while that incident is open: the job we already have, linked
                      (Event.near_incident), never a new lead. A safety category is linked and NOT held: a hazard at
                      our own scene is what a crew most needs to hear.
     When a hold ends (the place read back at one of our streets), the event gets back its rules' mode.
     A category with "place": true (rules.py; any pack) is held the same way while it has no place a crew can drive to
     (PLACED) in the source's towns, else the pack's area: "unplaced", or "out_of_area" for a place elsewhere. Only its
     own words or a read-back of the held call place it (_may_place); placed more than RELEASE_S after the call-out it
     stays at log ("placed_late"). A category with "defer_to" is never raised beside that category (_deferred), and an
     event of that category takes its open event over: one pre-alert per call. The tick never lets a held call go: an
     unplaced one stays at log until it expires (#640: a burning smell with no address is no pre-alert).
     A push can only be as early as the transcript: a row is read when its text lands (consider), so speech-to-text's
     queue (sizeup/stt.py: another system's "text" talkgroups wait in the LOW class) is the delay before a push, never
     the engine. A row transcribed after newer rows of its source is still read (Window.prune keeps it).
     The address lookup (step 4) runs only for a row that could carry a category to the threshold with the window's
     hits (a cue or a damper alone never makes an event, so it never pays for the gazetteer), and, for a pack with an
     area, only in those towns.
Intel never writes incidents, radio_links, incoming or alarms.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import queue
import threading
import time
from dataclasses import dataclass, field

from sizeup.intel import counters as counters_mod
from sizeup.intel import policy as policy_mod
from sizeup.intel import push as push_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel.model import Decision, Event, Hit, lowest, mode_rank
from sizeup.intel.policy import Policy, level

TICK_S = 15.0
PRUNE_S = 3600.0
PRUNE_FIRST_S = 3 * 3600.0             # the first prune this long after start (never in the startup's busy minutes)
UNIT_MENTION = re.compile(r"\b(car|unit|adam|boy|charlie|david|sector|post|rmp|patrol)\s+(\d{1,4}[a-z]?)\b", re.I)
# a new incident: only these ("another caller", "another one", "additional callers" are MORE of the same one)
NEW_CALL = re.compile(r"\b(new call|second call|another call|different (call|location|address))\b", re.I)
# a moving incident's new place ("now at", "last seen", "fled", "heading", "continuing", "still")
MOVE_WORDS = re.compile(r"\b(now at|now on|last seen|fled|fleeing|heading|continuing|still|turned onto|turning onto)\b", re.I)
# a dispatcher / HQ talker (they voice every job); never a unit's alias such as "RYE PD CAR 3"
DISPATCH_WORDS = re.compile(r"\bdisp\b|dispatch|\bhq\b|headquarters|\bcontrol\b|console|\bdesk\b", re.I)
PEOPLE = {"weapons", "edp", "dispute"}   # incidents about a person, who may move ("last seen at")
SAFETY = {"weapons", "barricade", "water_rescue", "fire_report"}   # never ended by a disregard that names nothing
DEESCALATED = "de-escalated"           # the note a category's "deescalate" phrase leaves on its open event
SAME_PLACE_M = 150.0                   # two places this close are the same incident's
NEAR_INCIDENT_M = 150.0                # an event this close to one of our open incidents is that incident
INCIDENT_BEFORE_S = 6 * 3600.0         # ... an incident opened at most this long before the event's first transmission
INCIDENT_AFTER_S = 30 * 60.0           # ... or at most this long after its last (the radio heard first, then our call)
PLACED = ("address", "intersection", "block", "landmark")     # a place a crew can drive to ("place": true categories)
RELEASE_S = 300.0                      # an "unplaced" hold: a read-back this soon after the call-out places it, and a
                                       # place found later than this updates the event at log ("placed_late"), unpushed
# a read-back of a held call: the address and these words only ("copy, that's going to be 40 Example Place"; "the
# house is 40 Example Place"; "40 Example Place, the caller's house"). The building's nouns are read-back filler; any
# other content word ("suspicious", "vehicle", "also ... when you clear") makes the row another job's.
READBACK_WORDS = frozenset({"copy", "copied", "that's", "thats", "that", "that'll", "is", "it's", "its", "it", "going",
                            "gonna", "to", "be", "correction", "correct", "repeat", "again", "the", "address", "at", "uh",
                            "um", "ok", "okay", "yeah", "yes", "roger", "10", "4", "sorry", "make", "number", "of",
                            "on", "in", "dispatch", "advised",
                            "house", "home", "apartment", "apt", "residence", "building", "caller", "caller's",
                            "callers"})
APT_WORDS = frozenset({"apartment", "apt"})    # ... and the number right after one ("unit 3" is a police unit's)
APT_NUMBER = re.compile(r"\d{1,4}[a-z]?|[a-z]\d{0,3}")
STOP_JOIN_S = 1.0                      # stop(): how long the thread may take to finish the row in hand
STREET_WORDS = frozenset({"street", "st", "avenue", "ave", "road", "rd", "drive", "dr", "lane", "ln", "place", "pl",
                          "parkway", "pkwy", "boulevard", "blvd", "turnpike", "tpke", "court", "ct", "way", "terrace",
                          "circle", "highway", "hwy", "plaza", "square"})
STOP_WORDS = {"from", "with", "that", "this", "the", "and", "into", "front", "near", "area", "call", "calls", "caller",
              "report", "reported", "respond", "units", "unit", "male", "female", "person", "subject", "someone"}
QUEUE_MAX = 5000
WHO_MAX = 500                          # unplaced events whose talkers and units are kept past the window
SEEN_MAX = 20000                       # traffic ids remembered with their text (the same text again: skipped)
ROW_SQL = ("SELECT id, start, end, source, system, service, dept, tier, talkgroup, channel, talker, rig, radio_id, text, "
           "alt_text, model FROM radio_traffic WHERE id = ?")
SECTIONS = ("intel", "intel_packs", "intel_rules")
SUMMARY_S = 3600.0                     # one "intel: considered N, in scope M, matched K, events E" line this often
COUNTERS_META = "intel_counters"       # the counters' hours, kept with that line (never written at startup)
PUSH_REFRESH_S = 60.0                  # push_sources(): recomputed at least this often (sources nothing tells us of)
PUSH_REV_BITS = 53                     # push_rev(): exact as a JSON number in JavaScript too (2^53)


@dataclass
class Entry:
    at: float
    hits: list                         # [(Rule, effective weight)]
    text: str
    who: tuple = ()                    # talker, radio ID, units: kept out of any push (scrub)
    suppressed: bool = False
    talker: str = ""                   # who keyed up ("" not known)
    units: frozenset = frozenset()     # the units it names or its radio is ("car 3"), lower case
    dispatcher: bool = False           # the dispatcher / HQ voices every job: never a tie by talker alone
    place: object = None               # the place in its own words (model.Place), or None


@dataclass
class Window:
    entries: dict = field(default_factory=dict)      # tid -> Entry

    def prune(self, newest: float, span: float, keep: str = "") -> None:
        """Entries older than span before the newest go; never `keep` (the row being read: a transmission transcribed
        late, after newer ones of its source, is still judged, with what the window holds, instead of dropped)."""
        for tid in [t for t, e in self.entries.items() if e.at < newest - span and t != keep]:
            del self.entries[tid]

    def ordered(self) -> list[tuple[str, Entry]]:
        return sorted(self.entries.items(), key=lambda x: (x[1].at, x[0]))


class RecordingSink:
    """A sink that only records (replay, tests)."""

    def __init__(self):
        self.hits: list[Hit] = []
        self.events: list[tuple[Event, str]] = []
        self.pushes: list[tuple[Event, Decision]] = []
        self.digests: list[list[Event]] = []

    def hit(self, h: Hit) -> None:
        self.hits.append(h)

    def event(self, ev: Event, change: str) -> None:
        self.events.append((copy.deepcopy(ev), change))

    def push(self, ev: Event, d: Decision) -> None:
        self.pushes.append((copy.deepcopy(ev), d))

    def digest(self, evs: list[Event]) -> None:
        self.digests.append(list(evs))


def _snapshot(settings) -> dict:
    s = settings() if callable(settings) else settings
    return {k: dict((s or {}).get(k) or {}) for k in SECTIONS}


class IntelEngine:
    def __init__(self, *, packs=None, settings=None, events=None, sink=None, locate=None, clock=time.time,
                 thread: bool = True, store=None, log=print, tz=None):
        self._settings_src = settings
        self._fixed_packs = packs
        self.store = store
        self.clock, self.log, self.tz = clock, log, tz
        self.locate = locate
        self.threaded = thread
        if events is None:
            if store is None:
                from sizeup.intel.events import MemoryEvents
                events = MemoryEvents()
            else:
                from sizeup.intel.events import SqlEvents
                events = SqlEvents(store)
        self.events = events
        self.policy = Policy(tz=tz)
        self.public_url = ""
        self.leader = lambda: True            # live: the CAD of record holding the lease (the app sets it)
        self.active = lambda: True            # live: False on a station forwarding to a hub (no thread runs there)
        self.notifier = None                  # live: notify.Pushes' INTEL KindNotifier (the app sets it)
        self.listen = None                    # live: listen.Listen, the push's "Listen" link (the app sets it)
        self.infer = None                     # infer.Inference: the model behind the rules (off unless set and on)
        # incidents(now) -> [{id, lat, lon, label, created_at, closed_at}]: our own open and recent incidents (the CAD
        # sets it; replay may), for near_incident. None: no incidents, nothing linked.
        self.incidents = None
        self.windows: dict[tuple[str, str], Window] = {}
        self.packs: list = []
        self.cfg: dict = {}
        self._hash = ""
        self._lock = threading.RLock()
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.dropped = 0
        self.stale = 0                        # rows too old to be a pre-alert (a backfill, a late re-record): skipped
        self._seen: dict[str, int] = {}
        self.errors = 0
        self.last_error = ""
        self._ticked = 0.0
        self._pruned = self.clock() + PRUNE_FIRST_S - PRUNE_S      # the first prune hours after start (#492 review)
        self._catwords: dict[tuple[str, str], set] = {}
        self._who_of: dict = {}               # unplaced event id -> (talkers, units) of its own transmissions
        self.counters = counters_mod.Counters(self.clock)
        self._summed = self.clock()           # the hourly log line: the first an hour after start
        self._summed_at: dict = {}            # the overall totals at that line
        self._led = False                     # the kept counters are read when this one first leads (a handover too)
        self.count_errors = 0                 # a counter that failed (never the transcript's problem: _count)
        # push_sources(): every source known here, as rows of the radio log (source_rows(); the app sets it). None:
        # no source is known, so none is one intel pushes from
        self.sources = None
        self._push_lock = threading.Lock()    # its own: never waits on a row being read (self._lock)
        self._push: frozenset | None = None
        self._push_rev = 0                    # push_rev(self._push); 0 until computed
        self._push_at = 0.0
        self._push_dirty = True
        self.reload()
        if sink is None:
            sink = LiveSink(self) if store is not None else RecordingSink()
        self.sink = sink

    # -- rules ------------------------------------------------------------------------------------------------------

    def reload(self) -> None:
        """The packs and settings again (after a change in Settings); a pack that doesn't compile: the files alone."""
        snap = _snapshot(self._settings_src)
        with self._lock:
            self.cfg = snap
            self.policy.configure(snap["intel"])
            if self._fixed_packs is not None:
                self.packs = list(self._fixed_packs)
            else:
                try:
                    self.packs = rules_mod.load_packs(None, snap)
                except (ValueError, OSError) as e:
                    self.packs = self._load_without_bad(snap, e)
            self._hash = rules_mod.ruleset_hash(self.packs)
            self._catwords = {}
        self.sources_changed()                # the packs and switches: push_sources() again
        self._watch()                         # a pack or detection turned on: watched from now (counters.py)

    def _load_without_bad(self, snap: dict, err) -> list:
        """The packs with every Settings override that compiles on its own (one bad or out-of-date key, say a town
        no longer listed, never drops the others: the police pack's tuning stays); each one left out is logged. If the
        rest still doesn't compile: the pack files alone."""
        good = {k: dict(v) for k, v in snap.items()}
        for sec in ("intel_packs", "intel_rules"):
            for key, val in list((snap.get(sec) or {}).items()):
                try:
                    rules_mod.load_packs(None, {sec: {key: val}})
                except (ValueError, OSError) as e:
                    good[sec].pop(key, None)
                    self.log(f"intel: Settings' {sec}/{key} not applied ({e})")
        try:
            return rules_mod.load_packs(None, good)
        except (ValueError, OSError) as e:
            self.log(f"intel: Settings' rule changes not applied ({err}; {e}): the pack files alone")
            return rules_mod.load_packs(None, {})

    def ruleset_hash(self) -> str:
        return self._hash

    @property
    def enabled(self) -> bool:
        return bool(self.policy.cfg.get("enabled", True))

    def pack(self, name: str):
        return next((p for p in self.packs if p.name == name), None)

    # -- push sources: speech-to-text's priority hook (the module's doc, "Push sources") ------------------------------

    def sources_changed(self) -> None:
        """Something push_sources() depends on changed (the packs, a Settings section, the known sources): the next
        call recomputes it. Cheap: nothing is computed here."""
        self._push_dirty = True

    def push_sources(self) -> tuple[frozenset[str], int]:
        """(the source keys intel could push from right now, push_rev() of them: a content hash, the same for the same
        set in any process). Cached; recomputed after sources_changed() / reload(), else at most PUSH_REFRESH_S after
        the last time."""
        with self._push_lock:
            now = time.monotonic()
            if self._push is None or self._push_dirty or now - self._push_at >= PUSH_REFRESH_S:
                self._push_dirty = False      # (before: a change while computing marks it again)
                self._push_at = now
                keys = self._push_keys()
                if keys != self._push:
                    self._push, self._push_rev = keys, push_rev(keys)
            return self._push, self._push_rev

    def is_push_source(self, channel_key: str, kind: str = "") -> bool:
        """The channel (as speech-to-text keys it: "tg:307", "p25:mrrs:9211", a bare "FG1") is one intel could push
        from now (push_sources(), cached), its key normalised as rows are keyed (source_key(); the module's doc)."""
        keys, _rev = self.push_sources()
        return any(k in keys for k in source_key(channel_key, kind))

    def _push_keys(self) -> frozenset[str]:
        if not self.enabled or not self.policy.cfg.get("push", True):
            return frozenset()                # intel off, or the kill switch: nothing is pushed
        n = self.notifier
        try:
            if n is None or not getattr(n, "enabled", True):
                return frozenset()            # no intel topic in effect: LiveSink sends nothing (#641)
        except Exception as e:  # noqa: BLE001 - the topic unknown: none (the push would fail closed too)
            self.log(f"intel: push sources: the topic: {type(e).__name__}")
            return frozenset()
        packs = [p for p in self.packs if can_push(p)]
        if not packs or self.sources is None:
            return frozenset()
        try:
            rows = list(self.sources())
        except Exception as e:  # noqa: BLE001 - the sources unreadable now: the set as it was
            self.log(f"intel: push sources: the known sources: {type(e).__name__}")
            return self._push if self._push is not None else frozenset()
        return frozenset(str(r.get("source") or "") for r in rows if any(p.in_scope(r) for p in packs))

    # -- the live queue ---------------------------------------------------------------------------------------------

    def consider(self, tid) -> None:
        """A transcript arrived or changed (Cad._on_text for our channels, Comms.on_context_text for context ones):
        queued, never waits. It runs on the caller's thread (transcription, the station-apply thread, the AWS
        callback): no rules, gazetteer, model or push here, and nothing written; the "intel" thread does all of that
        (through store.tx(), never under the caller's store.lock). Without a thread (replay): feed() rows directly."""
        if not self.threaded or not self.enabled or not self.active():
            return
        try:
            self._q.put_nowait(str(tid))
        except queue.Full:
            self.dropped += 1

    def start(self) -> None:
        if not self.threaded or (self._thread is not None and self._thread.is_alive()):
            return
        self._restore()
        self._thread = threading.Thread(target=self._run, name="intel", daemon=True)
        self._thread.start()
        self.log("intel: engine started (" + ", ".join(f"{p.name} {p.mode}" for p in self.packs) + ")")

    def stop(self) -> None:
        """No more rows: the thread ends after the one in hand (at most STOP_JOIN_S waited for, so a push it was making
        never lands after a drain has begun, and a slow one never holds the drain up)."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            try:
                self._q.put_nowait("")       # wakes its wait at once ("" is no row)
            except queue.Full:
                pass
            t.join(STOP_JOIN_S)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tid = self._q.get(timeout=TICK_S)
            except queue.Empty:
                tid = None
            try:
                if tid and not self._stop.is_set():
                    self._one(tid)
                if not self._stop.is_set():
                    self._maybe_tick()
            except Exception as e:  # noqa: BLE001 - a pre-alert never stops the thread
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                self.log(f"intel: {type(e).__name__}")
            finally:
                if tid is not None:
                    self._q.task_done()

    def drain(self) -> int:
        """Everything queued, here and now (tests; the app's thread does it live)."""
        n = 0
        while True:
            try:
                tid = self._q.get_nowait()
            except queue.Empty:
                return n
            try:
                self._one(tid)
                n += 1
            finally:
                self._q.task_done()

    def _one(self, tid: str) -> None:
        if not self.leader():                 # a standby, a fenced hub: the leader does it
            return
        row = self.store.one(ROW_SQL, (tid,)) if self.store is not None else None
        if row:
            self.feed(row)

    def _maybe_tick(self) -> None:
        now = self.clock()
        if now - self._ticked < TICK_S:
            return
        if not self.leader():
            self._led = False
            return
        self._ticked = now
        if not self._led:
            self._led = True
            self._watch(now)
            self._restore_counters(now)
        self.tick(now)
        if self.listen is not None:
            self.listen.refresh(now)          # later transmissions into the links already pushed
        if now - self._pruned >= PRUNE_S and hasattr(self.events, "prune"):
            self._pruned = now
            self.events.prune(now)
            if self.infer is not None:
                self.infer.prune(now)
        if now - self._summed >= SUMMARY_S:
            self._summary(now)

    def _restore(self) -> None:
        if self.store is None:
            return
        try:
            self.policy.restore(json.loads(self.store.meta_get("intel_policy") or "{}"))
        except (ValueError, TypeError):
            pass

    # -- the counters (sizeup/intel/counters.py): never in the way of a transcript -------------------------------------

    def _counting(self) -> bool:
        """This engine is the one that reads the transcripts: not a station forwarding to a hub, not a standby."""
        try:
            return bool(self.active()) and bool(self.leader())
        except Exception:  # noqa: BLE001
            return False

    def _watch(self, now: float | None = None) -> None:
        """Which packs are on and counted here, told to the counters (a pack newly so is watched from now)."""
        try:
            on = [p.name for p in self.packs if p.mode != "off"] if self.enabled and self._counting() else []
            self.counters.watching(self.clock() if now is None else now, on)
        except Exception:  # noqa: BLE001 - counters are a convenience
            self.count_errors += 1

    def _count(self, now: float, key: str, names=(), overall: bool = True) -> None:
        try:
            self.counters.add(now, key, names, overall)
        except Exception:  # noqa: BLE001 - a counter never costs an event
            self.count_errors += 1

    def _count_row(self, row: dict, now: float) -> list[str] | None:
        """A transcript handed over: considered, and in whose scope it is. The packs' names it is in scope of; None
        when the counting itself failed (then nothing more is counted for this row, and it is read as ever)."""
        try:
            packs = list(self.packs)
            scoped = [p.name for p in packs if p.in_scope(row)]
            ct = self.counters
            ct.add(now, "considered", [p.name for p in packs])
            ct.add(now, "in_scope", scoped, overall=bool(scoped))
            ct.add(now, "out_of_scope", [p.name for p in packs if p.name not in scoped], overall=not scoped)
            if scoped:
                ct.in_scope(now, scoped)      # when it was handed over, not when it was said (a catch-up is now)
            return scoped
        except Exception:  # noqa: BLE001 - a counter never costs an event
            self.count_errors += 1
            return None

    def _restore_counters(self, now: float) -> None:
        """The counters' hours, last-in-scope and watched-since times as last kept (a read; nothing is written here).
        A kept state too large to be ours is left alone: the counters start fresh, and the next hour's replaces it."""
        if self.store is None:
            return
        try:
            raw = self.store.meta_get(COUNTERS_META) or "{}"
            if len(raw) > counters_mod.STATE_MAX:
                self.log(f"intel: the kept counters are {len(raw)} characters: not read, starting fresh")
                return
            self.counters.restore(json.loads(raw), now, [p.name for p in self.packs])
        except Exception:  # noqa: BLE001 - counters are a convenience
            self.count_errors += 1

    def _summary(self, now: float) -> None:
        """The hourly line (the log shows the engine is alive, and whether anything is in scope), and the counters
        kept: one small meta row an hour, on the leader, never at startup."""
        self._summed = now
        try:
            line, self._summed_at = self.counters.hour_line(self._summed_at)
            self.log(line)
            if self.store is not None:
                raw = json.dumps(self.counters.state(now, [p.name for p in self.packs]))
                if len(raw) <= counters_mod.STATE_MAX:
                    self.store.meta_put(COUNTERS_META, raw)
        except Exception:  # noqa: BLE001 - counters are a convenience
            self.count_errors += 1

    def counts(self, now: float | None = None) -> dict:
        """{started, all: {h24, start}, packs: [counters.row() + {h24, start}]}: one terse line per pack."""
        now = self.clock() if now is None else now
        packs = list(self.packs)
        here = self._counting()
        self._watch(now)
        snap = self.counters.snapshot(now, [p.name for p in packs])
        rows = []
        for p in packs:
            c = snap["packs"][p.name]
            rows.append({**counters_mod.row(c, self.enabled and p.mode != "off", snap["started"], now, p.label, p.name,
                                            here=here), "h24": c["h24"], "start": c["start"]})
        return {"started": snap["started"], "all": snap["all"], "packs": rows}

    def save_policy(self) -> None:
        if self.store is not None:
            try:
                self.store.meta_put("intel_policy", json.dumps(self.policy.state()))
            except Exception:  # noqa: BLE001 - the bucket is a convenience
                pass

    # -- the pipeline -----------------------------------------------------------------------------------------------

    def feed(self, row: dict) -> list[Event]:
        """One transmission through every pack whose scope it is in. The events raised or changed."""
        if not self.enabled:
            return []
        tid = str(row.get("id") or "")
        text = str(row.get("text") or "")
        if not tid:
            return []
        # The hook fires again for the same row (every re-record, a startup backfill of old rows): a row older than
        # max_age_min is history, never a pre-alert (a restart must not replay old pushes); the same text again is
        # nothing new (idempotent per traffic id, and cheap)
        start = float(row.get("start") or 0.0)
        now = self.clock()
        # counted first (counters.py): in whose scope it is, so "nothing matched" and "nothing in scope" read apart
        scoped = self._count_row(row, now)
        if start and now - start > float(self.policy.cfg.get("max_age_min", 15)) * 60:
            self.stale += 1
            if scoped:
                self._count(now, "too_old", scoped)
            return []
        sig = hash((text, str(row.get("alt_text") or "")))
        if self._seen.get(tid) == sig:
            if scoped:
                self._count(now, "repeat", scoped)
            return []
        if scoped and policy_mod.quiet(self.policy.cfg, now, self.tz):
            self._count(now, "quiet", scoped)
        self._seen[tid] = sig
        if len(self._seen) > SEEN_MAX:
            for k in list(self._seen)[: SEEN_MAX // 2]:
                del self._seen[k]
        out: list[Event] = []
        with self._lock:
            for pack in self.packs:
                if pack.in_scope(row):
                    out += self._feed_pack(pack, row, tid, text)
        return out

    def _feed_pack(self, pack, row: dict, tid: str, text: str) -> list[Event]:
        at = float(row.get("start") or self.clock())
        key = str(row.get("source") or row.get("source_key") or "")
        ws = rules_mod.words(text)
        suppressed = pack.suppressed(text, ws)
        hits: list = []
        if not suppressed:
            got = set()
            for r in pack.rules:
                if r.mode != "off" and r.hit(text, ws):
                    hits.append((r, r.w))
                    got.add(r.id)
            alt = str(row.get("alt_text") or "")
            if alt and pack.context_factor > 0:
                aws = rules_mod.words(alt)
                if not pack.suppressed(alt, aws):
                    hits += [(r, r.w * pack.context_factor) for r in pack.rules
                             if r.id not in got and r.mode != "off" and r.hit(alt, aws)]
        who = tuple(x for x in (str(row.get("talker") or ""), str(row.get("radio_id") or ""),
                                *str(row.get("rig") or "").split(",")) if x.strip())
        win = self.windows.setdefault((pack.name, key), Window())
        known = tid in win.entries
        talker = str(row.get("talker") or "").strip()
        units = {f"{m.group(1).lower()} {m.group(2).lower()}" for m in UNIT_MENTION.finditer(text)}
        units |= {u.strip().lower() for u in str(row.get("rig") or "").split(",") if u.strip()}
        disp = bool(row.get("dispatcher")) or bool(talker and (DISPATCH_WORDS.search(talker) or talker in self._hq(row)))
        if talker and not disp and UNIT_MENTION.fullmatch(talker):
            units.add(talker.lower())
        entry = Entry(at, hits, text, who, suppressed, talker, frozenset(units), disp)
        if self.locate is not None and not suppressed and (
                self._could_reach(pack, win, tid, hits)
                or any(not e.confirmed or pack.category(e.category).get("moves")
                       or (e.category in PEOPLE and MOVE_WORDS.search(text))
                       for e in self._open_for(pack, None, key, at, self._merge_span(pack)))):
            entry.place = self._where(pack, row, key, [text], list(who))
        win.entries[tid] = entry
        win.prune(max(e.at for e in win.entries.values()), pack.window_s, keep=tid)
        changed: list[Event] = []
        if suppressed:
            changed += self._end_window(pack, key, at, tid, win, ws)
        cats = self._scores(pack, win)
        mine = {r.category for r, _w in hits}
        event_of: dict[str, Event] = {}
        over = False                          # a category this row speaks of reached its threshold
        for cat, (what, contrib, best_mode) in cats.items():
            cd = pack.category(cat)
            thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
            if what < thr:
                continue
            if not (mine & {cat, rules_mod.WILD, "routine"}):
                continue                      # a candidate this row says nothing about: left as it is
            over = True
            mode = lowest(pack.mode, cd.get("mode", "push"), best_mode)
            if mode == "off":
                continue
            dfr = self._deferred(pack, cat, cats, key, at, win.entries[tid])
            if isinstance(dfr, Event):       # the call it defers to, unplaced: this row's address is its place
                up = self._set_place(pack, dfr, win.entries[tid].place, key, tid, at)
                if up is not None:
                    event_of[up.category] = up
                    changed.append(up)
                continue
            if dfr:
                continue
            ev = self._raise(pack, row, key, cat, cd, what, contrib, mode, win, at, tid)
            if ev is not None:
                event_of[cat] = ev
                changed.append(ev)
        if not suppressed and pack.calm:
            for x in (self._uncalm(pack, key, at, ws, win.entries[tid], event_of)
                      + self._deescalate(pack, row, key, at, tid, text, ws, win.entries[tid])):
                if not any(x is c for c in changed):
                    changed.append(x)
        if hits and pack.mode != "off":
            self._count(self.clock(), "matched", [pack.name])
            if not over:
                self._count(self.clock(), "below", [pack.name])
        if not hits and not suppressed:       # a read-back with no rule words: may still place an open event
            for ev in self._open_for(pack, None, key, at, self._merge_span(pack)):
                moving = self._moving(pack, ev, win.entries[tid])
                if (ev.category not in event_of and (not ev.confirmed or moving) and self.locate is not None
                        and (self._may_place(pack, ev, win.entries[tid], False) if ev.suppressed == "unplaced"
                             else self._same_job(pack, ev, win.entries[tid], ws, mine))):
                    up = self._relocate(pack, ev, row, key, win, tid, at)
                    if up is not None:
                        changed.append(up)
        if self.infer is not None and hits and not suppressed and pack.src.get("infer", True):   # ("infer": false: rules only)
            try:
                changed += self._inference(pack, row, key, tid, win, at, cats, mine, event_of, hits)
            except Exception as e:  # noqa: BLE001 - the model never stops the rules
                self.log(f"intel: inference: {type(e).__name__}")
        keep = {r.id for r, _w in hits}
        if known and hasattr(self.sink, "clear"):
            self.sink.clear(tid, keep)
        for r, w in hits:
            ev = event_of.get(r.category)
            if ev is None and r.category in (rules_mod.WILD, "routine") and len(event_of) == 1:
                ev = next(iter(event_of.values()))
            self.sink.hit(Hit(tid, r.id, r.v, round(float(w), 4), r.category, pack.name, at, ev.id if ev else None))
        return changed

    def _could_reach(self, pack, win: Window, tid: str, hits: list) -> bool:
        """This row's hits could carry one of the categories it says to the threshold, with the window's strongest
        hits of that category and the cues (dampers aside: an upper bound). Else no gazetteer work for it."""
        cues = [w for r, w in hits if r.category == rules_mod.WILD and w > 0]
        for cat in {r.category for r, w in hits if w > 0 and r.category in pack.categories}:
            ws = [w for r, w in hits if r.category == cat and w > 0] + cues
            best: dict[str, float] = {}
            for t, e in win.entries.items():
                if t == tid or e.suppressed:
                    continue
                for r, w in e.hits:
                    if r.category == cat and w > 0 and r.mode != "shadow":
                        best[r.id] = max(best.get(r.id, 0.0), w)
            miss = 1.0
            for w in ws + list(best.values()):
                miss *= 1.0 - min(1.0, w)
            cd = pack.category(cat)
            thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
            if 1.0 - miss >= thr - 1e-9:
                return True
        return False

    def _scores(self, pack, win: Window) -> dict:
        """{category: (WHAT, [(tid, rule, w)], the rules' rung)} over the window (the rung: step 5 of the module's doc)."""
        best: dict[str, tuple[float, str, object]] = {}      # rule id -> (weight, tid, rule): its strongest in the window
        for tid, e in win.ordered():
            if e.suppressed:
                continue
            for r, w in e.hits:
                if r.mode == "shadow":
                    continue                  # counted (sink.hit), never scored
                if r.id not in best or abs(w) > abs(best[r.id][0]):
                    best[r.id] = (w, tid, r)
        # a cue or a damper counts only in a transmission that says the category itself: the dispatcher's "parking
        # complaint" or "respond to" for another job never moves this one (#492 re-check)
        per_tid: dict[str, list] = {}
        for tid, e in win.ordered():
            if not e.suppressed:
                for r, w in e.hits:
                    if r.mode != "shadow":
                        per_tid.setdefault(tid, []).append((w, tid, r))
        out = {}
        for cat in pack.categories:
            pos = [(w, tid, r) for w, tid, r in best.values() if r.category == cat and w > 0]
            if not pos:
                continue
            said = {t for _w, t, _r in pos} | {t for t, hs in per_tid.items() if any(r.category == cat and w > 0 for w, _t, r in hs)}
            side: dict[str, tuple] = {}
            for t in said:
                for w, _t, r in per_tid.get(t, []):
                    if (r.category == rules_mod.WILD and w > 0) or w < 0:
                        if r.id not in side or abs(w) > abs(side[r.id][0]):
                            side[r.id] = (w, t, r)
            cues = [x for x in side.values() if x[0] > 0]
            damp = [x for x in side.values() if x[0] < 0]
            miss = 1.0
            for w, _t, _r in pos + cues:
                miss *= 1.0 - min(1.0, w)
            what = 1.0 - miss
            for w, _t, _r in damp:
                what *= 1.0 + max(-1.0, w)
            cd = pack.category(cat)
            thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
            top_mode = rung(pos, damp, thr, cues)
            out[cat] = (round(what, 4), [(t, r, w) for w, t, r in pos + cues + damp], top_mode)
        return out

    def _open_for(self, pack, cat: str | None, key: str, at: float, span: float | None = None) -> list[Event]:
        span = pack.window_s if span is None else span
        return [e for e in self.events.open_events()
                if e.pack == pack.name and e.source_key == key and (cat is None or e.category == cat)
                and e.last_at >= at - span and (not e.until or e.until >= at)]

    def _merge_span(self, pack) -> float:
        """How far back an open event of the same pack, category and source takes a new mention (#492 review, H3)."""
        return max(pack.window_s, float(self.policy.cfg.get("merge_min", 20)) * 60)

    def category_words(self, pack, cat: str) -> set:
        """The telling words of a category's rules ("smoke", "flames", "shots" ...)."""
        k = (pack.name, cat)
        if k not in self._catwords and pack.category(cat).get("words"):
            self._catwords[k] = set(pack.category(cat)["words"])     # its own list (fire_call: never "have", "check")
        if k not in self._catwords:
            out = set()
            for r in pack.rules:
                if r.category != cat:
                    continue
                for ph in r.any:
                    for item in ph.items or ():
                        if item == rules_mod.GAP:
                            continue
                        for alt in item:
                            out |= {w for w in alt if len(w) >= 4 and w not in STOP_WORDS}
            self._catwords[k] = out
        return self._catwords[k]

    def _hq(self, row: dict) -> set:
        from sizeup.intel.place import callsigns
        return set(callsigns(str(row.get("system") or ""), str(row.get("service") or "")))

    def _event_entries(self, pack, ev: Event) -> list:
        win = self.windows.get((pack.name, ev.source_key))
        return [win.entries[t] for t in ev.traffic_ids if win is not None and t in win.entries]

    def _same_job(self, pack, ev: Event, entry, ws: list[str], mine=frozenset()) -> bool:
        """A transmission is about this event only when (#492 re-check):
             it names a unit or car already tied to the event ("car 3 to dispatch, I'm at ..."),
             or repeats the event's category words,
             or reads back the event's place,
             or the same talker said it, when that talker is known and is no dispatcher or HQ (who voices every job).
        A row with another category's rule words is another job."""
        if mine - {ev.category, rules_mod.WILD, "routine"}:
            return False
        if self.category_words(pack, ev.category) & set(ws):
            return True
        if entry.place is not None and ev.precision != "none" and same_place(entry.place, ev.place()):
            return True
        mine_entries = self._event_entries(pack, ev)
        shared = entry.units & frozenset(u for e in mine_entries for u in e.units)
        if shared and not self._busy(pack, ev, shared):
            return True                       # (a unit on two or more open jobs: only words or the place tie it)
        if (entry.talker and not entry.dispatcher
                and entry.talker in {e.talker for e in mine_entries if e.talker and not e.dispatcher}):
            return True
        # "now at", "last seen at": the one moving incident open on this source
        movable = lambda e: pack.category(e.category).get("moves") or e.category in PEOPLE      # noqa: E731
        if MOVE_WORDS.search(entry.text) and movable(ev):
            return [e.id for e in self._open_for(pack, None, ev.source_key, entry.at, self._merge_span(pack))
                    if movable(e)] == [ev.id]
        return False

    def _busy(self, pack, ev: Event, units) -> bool:
        """These units are on another open job too (then they tie by words or place only)."""
        for e in self._open_for(pack, None, ev.source_key, self.clock(), self._merge_span(pack)):
            if e.id != ev.id and units & frozenset(u for x in self._event_entries(pack, e) for u in x.units):
                return True
        return False

    def _moving(self, pack, ev: Event, entry) -> bool:
        """A row that moves this event's place: a moving category (pursuit), or the words of a move ("last seen",
        "fled", "now at") about a person (a suspect, an EDP); a fire, a crash, an alarm never move."""
        if pack.category(ev.category).get("moves"):
            return True
        return ev.category in PEOPLE and bool(MOVE_WORDS.search(entry.text))

    def _texts(self, pack, key: str, tids) -> list[str]:
        win = self.windows.get((pack.name, key))
        if win is None:
            return []
        return [win.entries[t].text for t in tids if t in win.entries and win.entries[t].text]

    def _window_text(self, win: Window) -> str:
        return " . ".join(e.text for _t, e in win.ordered() if e.text and not e.suppressed)

    def _scope(self, pack, row: dict, key: str) -> dict:
        own = pack.towns_for(key)
        return {"source_key": key, "system": str(row.get("system") or ""), "service": str(row.get("service") or ""),
                "towns": own or list(pack.area), "only": bool(pack.area) and not own, "pack": pack.name}

    def _outside(self, pack, ev: Event) -> bool:
        """A pack with an area: the event is elsewhere (its place's town isn't one of the area's; or, with no place,
        its transmissions name towns and none of the area's). No place and no town said: not known, so not outside."""
        if not pack.area:
            return False
        area = {t.lower() for t in pack.area}
        if ev.precision != "none" and ev.town:
            return ev.town.lower() not in area
        if pack.category(ev.category).get("safety"):
            return False                      # unplaced: a town said is no reason to hold a hazard back
        named = towns_said(self._texts(pack, ev.source_key, ev.traffic_ids))
        return bool(named) and not (named & area)

    def _incident(self, ev: Event) -> int | None:
        """Our own incident at the event's place, open around its time (same label, or within NEAR_INCIDENT_M)."""
        if self.incidents is None or ev.precision == "none" or ev.lat is None or ev.lon is None:
            return None
        try:
            incs = self.incidents(self.clock()) or []
        except Exception as e:  # noqa: BLE001 - no incidents is still a pre-alert
            self.log(f"intel: incidents: {type(e).__name__}")
            return None
        best = None
        for inc in incs:
            at = float(inc.get("created_at") or 0.0)
            closed = inc.get("closed_at")
            if not at or at < ev.first_at - INCIDENT_BEFORE_S or at > ev.last_at + INCIDENT_AFTER_S:
                continue
            if closed is not None and float(closed) < ev.first_at:
                continue
            same = bool(ev.label_full) and str(inc.get("label") or "").strip().lower() == ev.label_full.strip().lower()
            if not same and inc.get("lat") is not None and inc.get("lon") is not None:
                d = _m(ev.lat, ev.lon, float(inc["lat"]), float(inc["lon"]))
                same = d <= NEAR_INCIDENT_M + float(ev.radius_m or 0.0) / 2
            if same and (best is None or at > best[0]):
                best = (at, int(inc["id"]))
        return best[1] if best else None

    def _hold(self, pack, ev: Event, mode: str | None = None) -> None:
        """Out of area, or our own incident's: held at log (the module's doc, step 6). Only a pack with an area (the
        county-wide packs): the police pack's events are never linked, and held only in a "place": true category
        (unplaced, _unplaced). An event already pushed is never held
        (its mode never goes down: it stays on the map and the console). mode: what the rules say now, given back when
        a hold ends (a moving event leaving our incident's place)."""
        if not pack.area and not pack.category(ev.category).get("place"):
            return
        if ev.suppressed == "placed_late" and ev.pushed_at is None:
            ev.mode = lowest(ev.mode, "log")  # placed too late to be a pre-alert: stays at log
            return
        if pack.area:
            ev.near_incident = self._incident(ev)
        why = "out_of_area" if self._outside(pack, ev) else self._unplaced(pack, ev)
        if not why and ev.near_incident is not None and not pack.category(ev.category).get("safety"):
            why = "near_incident"
        if why and ev.pushed_at is None:
            ev.mode = lowest(ev.mode, "log")
            ev.suppressed = why
        elif ev.suppressed == "unplaced":
            # placed: the mode its rules raised it at (the window may have let the call-out go: never None here),
            # unless the place came too long after the call-out to be a pre-alert
            if ev.last_at - ev.first_at > RELEASE_S:
                ev.suppressed = "placed_late"
                ev.mode = lowest(ev.mode, "log")
            else:
                ev.suppressed = ""
                ev.mode = max(ev.mode, mode if mode is not None else self._event_mode(pack, ev), key=mode_rank)
        elif ev.suppressed in ("out_of_area", "near_incident"):
            ev.suppressed = ""
            if mode is not None:
                ev.mode = max(ev.mode, mode, key=mode_rank)

    def _event_mode(self, pack, ev: Event) -> str:
        """The mode the event's own rules (ev.rules, with their weights then) give it: pack, category and rung."""
        by = {r.id: r for r in pack.rules}
        got = [(float(w), "", by[i]) for i, _v, w in ev.rules if i in by]
        pos = [x for x in got if x[2].category == ev.category and x[0] > 0]
        cues = [x for x in got if x[2].category == rules_mod.WILD and x[0] > 0]
        damp = [x for x in got if x[0] < 0]
        cd = pack.category(ev.category)
        thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
        return lowest(pack.mode, cd.get("mode", "push"), rung(pos, damp, thr, cues)) if pos else "log"

    def _may_place(self, pack, ev: Event, entry, speaks: bool) -> bool:
        """An "unplaced" event takes a place only from a row that says its category itself, or from a read-back of
        the held call: within RELEASE_S of it, no other job's cue (no hit of another category, no dispatch cue, no
        unit the call never named), and said by the call's own talker (the dispatcher repeating the address) or by a
        unit the call named. Never by a talker's next job ("car 9 respond to ..., a dispute")."""
        if speaks:
            return True
        if entry.at - ev.first_at > RELEASE_S or any(r.category != ev.category for r, _w in entry.hits):
            return False
        talkers, units = self._held_who(pack, ev)
        if entry.units - units or not self._address_only(entry):
            return False
        return bool(entry.talker and entry.talker in talkers) or bool(entry.units & units)

    @staticmethod
    def _address_only(entry) -> bool:
        """The row is the address and nothing else: its place's words (number, street, town), street words, the units
        it names and READBACK_WORDS. Anything more ("they have a suspicious vehicle", "also ... when you clear") is
        another job's, never the held call's read-back.
        A known gap: the dispatcher's next job said as a bare address ("21 Sample Street, Port Chester.") by the same
        talker within RELEASE_S reads exactly like a read-back and places the held call there. The words can't tell
        them apart; the one cheap guard is _unplaced's: an address outside the source's towns stays held
        ("out_of_area"), never pushed."""
        if entry.place is None or not entry.place.label:
            return False
        ok = set(rules_mod.words(entry.place.label)) | STREET_WORDS | READBACK_WORDS
        ok |= {w for u in entry.units for w in u.split()}
        ws = rules_mod.words(entry.text)
        # "apartment 3", "apt 2b": the address's own apartment, read back with it
        apt = {i + d for i in range(len(ws) - 1) if ws[i] in APT_WORDS and APT_NUMBER.fullmatch(ws[i + 1]) for d in (0, 1)}
        return all(w in ok or i in apt for i, w in enumerate(ws))

    def _held_who(self, pack, ev: Event) -> tuple[set, frozenset]:
        """The talkers and units of an unplaced event's own transmissions (kept past the window, _note_who)."""
        talkers, units = self._who_of.get(ev.id, (set(), frozenset()))
        mine = [e for e in self._event_entries(pack, ev)]
        return (talkers | {e.talker for e in mine if e.talker},
                units | frozenset(u for e in mine for u in e.units))

    def _note_who(self, pack, ev: Event) -> None:
        if ev.suppressed == "unplaced" and ev.id is not None:
            self._who_of[ev.id] = self._held_who(pack, ev)
            while len(self._who_of) > WHO_MAX:
                self._who_of.pop(next(iter(self._who_of)))
        else:
            self._who_of.pop(ev.id, None)

    def _deferred(self, pack, cat: str, cats: dict, key: str, at: float, entry):
        """One pre-alert per call: a category that defers to another ("defer_to": fire_call to fire_report) is not
        raised while that one is over its threshold in this window, or has an open event on this source at this place
        (or either has none). The other way round, _raise takes the deferring category's open event over.
        False: not deferred; True: deferred; an Event: deferred to that open event, which has no place while this row
        has one (the caller gives it the place: "location unknown", then the address).
        The rows that have a place, and the open event that has one, defer only when the places match."""
        to = pack.category(cat).get("defer_to")
        if not to:
            return False
        for e in self._open_for(pack, to, key, at, self._merge_span(pack)):
            if e.precision == "none":
                return e if entry.place is not None and entry.place.precision in PLACED else True
            if entry.place is None or same_place(entry.place, e.place()):
                return True
        tcd = pack.category(to)
        thr = tcd.get("threshold") if tcd.get("threshold") is not None else pack.threshold
        return to in cats and cats[to][0] >= thr

    def _unplaced(self, pack, ev: Event) -> str:
        """A "place": true category (rules.py): "unplaced" until the event has a place a crew can drive to (PLACED),
        "out_of_area" when that place is outside the source's towns (else the pack's area); "" otherwise."""
        if not pack.category(ev.category).get("place"):
            return ""
        if ev.precision not in PLACED:
            return "unplaced"
        towns = {t.lower() for t in (pack.towns_for(ev.source_key) or pack.area)}
        return "out_of_area" if towns and (ev.town or "").lower() not in towns else ""

    def _rules_mode(self, pack, ev: Event) -> str | None:
        """The mode the window's rules give the event's category now (pack, category and rung), None: not reached."""
        win = self.windows.get((pack.name, ev.source_key))
        got = self._scores(pack, win).get(ev.category) if win is not None else None
        if got is None:
            return None
        cd = pack.category(ev.category)
        thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
        return lowest(pack.mode, cd.get("mode", "push"), got[2]) if got[0] >= thr else None

    def _where(self, pack, row: dict, key: str, texts: list[str], who=()):
        """The place in these transmissions' words (only the event's own: never another job's in the same window);
        a label the scrub refuses (a plate or an ID read as an address) is no place at all (fail closed)."""
        if self.locate is None or not texts:
            return None
        try:
            place = self.locate(" . ".join(texts), self._scope(pack, row, key))
        except Exception as e:  # noqa: BLE001 - no place is still a pre-alert
            self.log(f"intel: locate: {type(e).__name__}")
            return None
        if place is not None and push_mod.scrub_label(place.label, who):
            self.log("intel: a place withheld by the scrub")
            return None
        if place is not None and pack.area and not place.confirmed and place.town in pack.area:
            # a county-wide source's street, read unconfirmed into one of our towns: the gazetteer holds our area only,
            # so a "Main Street" anywhere in the county reads as ours (replay: most such readings were elsewhere).
            # Only a confirmed reading (a cross street, a read-back) places it here; one in another town still says
            # it is out of the area
            return None
        return place

    def _raise(self, pack, row, key, cat, cd, what, contrib, mode, win, at, tid) -> Event | None:
        now = self.clock()
        entry = win.entries[tid]
        speaks = any(r.category == cat and w > 0 for r, w in entry.hits)     # the row itself says this category
        rp = entry.place if speaks else None
        fresh = bool(NEW_CALL.search(entry.text))
        ev, moved, reopened = None, False, False
        span = self._merge_span(pack)
        under = [e for e in self._open_for(pack, None, key, at, span)               # a deferring category's call:
                 if e.category != cat and pack.category(e.category).get("defer_to") == cat]   # taken over (one per call)
        for e in self._open_for(pack, cat, key, at, span) + under:
            if e.suppressed == "unplaced" and not self._may_place(pack, e, entry, speaks):
                continue                      # a held call takes only its own words or its read-back
            ep = e.place() if e.precision != "none" else None
            if rp is not None and ep is not None and same_place(rp, ep):
                ev = e                        # the same place again ("another caller ...": more of the same)
            elif fresh:
                continue                      # "new call": another place, or none said: a new incident
            elif rp is None and (speaks or self._same_job(pack, e, entry, rules_mod.words(entry.text), {cat})):
                ev = e                        # no place said ("still smoke showing"): the open one (#492 re-check)
            elif rp is not None and ep is None and (e.last_at >= at - pack.window_s or e.suppressed == "unplaced"):
                ev = e                        # the place, right after a call-out that had none (or held for one)
            elif (rp is not None and ep is not None and self._moving(pack, e, entry)
                  and (cd.get("moves") or self._same_job(pack, e, entry, rules_mod.words(entry.text), {cat}))):
                ev, moved = e, True           # a moving incident ("now at", a pursuit): its place moves
            if ev is not None:
                break
        if ev is not None and ev.note == "possibly cancelled" and speaks:
            reopened = True                   # a nameless disregard, then the call again: it stands, pushed again
            ev.note = "re-opened"
        elif ev is not None and ev.note == DEESCALATED and speaks:
            ev.note = ""                      # the weapon is back in it: the note goes; a safety category this row alone
            if cd.get("safety") and self._alone(pack, entry, cat) >= self._thr(pack, cat):   # carries over its threshold
                reopened, ev.note = True, "re-opened"                                         # ("he grabbed a gun") pushes again
        if ev is None and not speaks:
            return None                       # a cue alone (another job's "respond to ...") never makes an event
        # its traffic: the rows that say this category (and this one when it ties), never another job's cue
        tids = list(dict.fromkeys([*(t for t, r, _w in contrib if r.category == cat and _w > 0),
                                   *([tid] if speaks or (ev is not None and self._same_job(
                                       pack, ev, entry, rules_mod.words(entry.text), {cat})) else [])]))
        place = None
        if ev is None:
            place = rp if rp is not None else self._where(
                pack, row, key, self._texts(pack, key, tids), [w for t in tids if t in win.entries for w in win.entries[t].who])
        elif (not ev.confirmed or moved) and tid in tids:
            place = rp if rp is not None else entry.place
        subject = f"{pack.name}:{cat}:{place.key}" if place is not None and place.key else ""
        if ev is None and subject and not fresh:
            ev = next((e for e in self.events.open_events() if e.subject == subject), None)
        if ev is None and not fresh:
            # a person's Dismiss holds: a new mention of a dismissed event's subject within mute_min is recorded at log,
            # never re-opened or pushed (only a nameless disregard's "possibly cancelled" event comes back, above)
            mute = float(self.policy.cfg.get("mute_min", 15)) * 60
            for c in self.events.recent_closed(pack.name, cat, now - mute) if mute else []:
                cp = c.place() if c.precision != "none" else None
                if c.status == "dismissed" and ((place is None and cp is None and c.source_key == key) or (
                        place is not None and cp is not None and same_place(place, cp))):
                    mode = lowest(mode, "log")
                    break
        before = copy.deepcopy(ev) if ev is not None else None
        if ev is not None and ev.category != cat:
            ev.category = cat                 # a deferring category's call, now this one's (one pre-alert per call)
            if ev.suppressed == "unplaced":
                ev.suppressed = ""            # (held for its old category's sake: _hold judges it as this one)
        rules = sorted({(r.id, r.v, round(float(w), 4)) for _t, r, w in contrib})
        if ev is None:
            ev = Event(domain=pack.domain, pack=pack.name, category=cat, source_key=key,
                       system=str(row.get("system") or ""), service=str(row.get("service") or ""),
                       dept=str(row.get("dept") or ""), first_at=at, status="open")
            change = "raised"
        else:
            change = "reopened" if reopened else "updated"
        ev.last_at = max(ev.last_at, at)
        ev.what_score = round(max(what, ev.what_score if change == "updated" else 0.0), 4)
        ev.level = level(ev.what_score)
        ev.rules = [list(x) for x in sorted({tuple(x) for x in ev.rules} | set(rules))] if change == "updated" else [list(x) for x in rules]
        ev.ruleset_hash = self._hash
        ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, *tids]))
        ev.mode = mode if change == "raised" else max(ev.mode, mode, key=mode_rank)
        ev.priority = max(int(cd.get("priority", 3)), ev.priority if change == "updated" else 0)
        ev.until = max(ev.until, ev.last_at + float(cd.get("ttl_min", 45)) * 60)
        if place is not None and (moved or ev.precision == "none" or place.confirmed or place.where_score > ev.where_score):
            ev.set_place(place)
            if moved:
                ev.note = "moved"
        ev.subject = f"{pack.name}:{cat}:{ev.place().key or key}"
        self._hold(pack, ev, mode)
        if before is not None and _same(before, ev):
            return None                       # idempotent: the same row again changes nothing
        ev.updated_at = now
        self.events.save(ev)
        self._note_who(pack, ev)
        self._after(pack, ev, change, now, before, moved=moved and before is not None and before.precision != "none"
                    and not same_place(before.place(), ev.place()))
        return ev

    def _relocate(self, pack, ev: Event, row, key, win, tid, at) -> Event | None:
        entry = win.entries[tid]
        place = entry.place                            # its own words only (a tied row: _same_job said so)
        move = (place is not None and ev.precision != "none" and not same_place(place, ev.place())
                and self._moving(pack, ev, entry))
        return self._set_place(pack, ev, place, key, tid, at, move)

    def _set_place(self, pack, ev: Event, place, key, tid, at, move: bool = False) -> Event | None:
        if place is None or not (move or ev.precision == "none" or place.confirmed or place.where_score > ev.where_score):
            return None
        before = copy.deepcopy(ev)
        ev.set_place(place)
        if move:
            ev.note = "moved"
        ev.last_at = max(ev.last_at, at)
        ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
        ev.subject = f"{pack.name}:{ev.category}:{place.key or key}"
        self._hold(pack, ev, self._rules_mode(pack, ev))
        if _same(before, ev):
            return None
        ev.updated_at = self.clock()
        self.events.save(ev)
        self._note_who(pack, ev)
        self._after(pack, ev, "updated", ev.updated_at, before, moved=move)
        return ev

    def _inference(self, pack, row, key, tid, win, at, cats, mine, event_of, hits) -> list[Event]:
        """Ask the model about what the rules left uncertain (infer.py). Never waits for it: the answer comes back in
        _infer_done (on the infer thread live, inline in replay). A sample is asked and recorded, never acted on."""
        from sizeup.intel import infer as infer_mod
        c = self.infer.cfg()
        why, ev_for, unsure = "", None, ""
        for cat, (what, _contrib, _m) in cats.items():
            cd = pack.category(cat)
            thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
            if float(c["infer_low"]) <= what < thr and mine & {cat, rules_mod.WILD}:
                why, unsure = "uncertain", cat
                break
        if not why:
            now = self.clock()
            ev_for = next((e for e in event_of.values() if (e.precision == "none" or not e.confirmed)
                           and now - self.infer.asked.get(e.id or 0, -1e18) >= infer_mod.PLACE_REASK_S), None)
            if ev_for is not None:
                why = "place"
                self.infer.asked[ev_for.id or 0] = now
        if not why and int(c["infer_sample_pct"]) and int(hashlib.sha256(tid.encode()).hexdigest(), 16) % 100 < int(c["infer_sample_pct"]):
            why = "sample"
        if not why:
            return []
        text = self._window_text(win)
        req = infer_mod.Request(text, {k: v["label"] for k, v in pack.categories.items()}, pack.towns_for(key), key)
        job = {"pack": pack.name, "key": key, "tid": tid, "row": dict(row), "at": at, "why": why, "unsure": unsure,
               "event_id": ev_for.id if ev_for else None, "text": text}
        self.infer.submit(req, job, self._infer_done)
        return []

    def _infer_done(self, job: dict, res) -> list[Event]:
        """The model's answer (#504 review): its category backs the rules (a push only when they agree, else
        infer_alone_mode); its place only if its words are in the transcript and the gazetteer finds it, UNCONFIRMED,
        never over a place the rules confirmed."""
        from sizeup.intel import infer as infer_mod
        if res is None or res.refused or job["why"] == "sample" or not self.leader():
            return []
        out: list[Event] = []
        with self._lock:
            pack = self.pack(job["pack"])
            if pack is None:
                return []
            c = self.infer.cfg()
            key, tid, at, row = job["key"], job["tid"], job["at"], job["row"]
            win = self.windows.get((pack.name, key))
            cat = res.category
            target = self.events.get(job["event_id"]) if job["event_id"] else None
            if (job["why"] == "uncertain" and cat in pack.categories and res.confidence > 0 and win is not None
                    and tid in win.entries):
                own = self._scores(pack, win).get(cat)                 # the rules' OWN score, before the model's
                # "agree" may push only when the rules already say this much themselves and the model is sure: the model
                # never escalates on its own (an MVA into an MVA with injuries, an alarm into a fire) (#504 re-check)
                agree = (cat == job["unsure"] and bool(c["infer_agree_push"]) and own is not None
                         and own[0] >= float(c["infer_agree_min"]) and res.confidence >= float(c["infer_agree_conf"]))
                rule = rules_mod.Rule(f"infer.{c['infer_provider']}", 1, cat,
                                      round(min(1.0, res.confidence * float(c["infer_weight"])), 4), [], [],
                                      str(c["infer_mode"]) if agree else lowest(str(c["infer_mode"]), str(c["infer_alone_mode"])),
                                      True, {})
                entry = win.entries[tid]
                entry.hits = [*entry.hits, (rule, rule.w)]        # a new list: the row's own report has its hits already
                self.sink.hit(Hit(tid, rule.id, rule.v, rule.w, cat, pack.name, at, None))
                got = self._scores(pack, win).get(cat)
                cd = pack.category(cat)
                thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
                if got and got[0] >= thr:
                    mode = lowest(pack.mode, cd.get("mode", "push"), got[2])
                    if not agree:
                        mode = lowest(mode, str(c["infer_alone_mode"]))        # the model alone: never a push
                    if mode != "off":
                        ev = self._raise(pack, row, key, cat, cd, got[0], got[1], mode, win, at, tid)
                        if ev is not None:
                            out.append(ev)
                            target = ev
            if target is None or target.status != "open" or target.confirmed or not res.location or self.locate is None:
                return out
            loc = infer_mod.said(res.location, job["text"])
            if not loc:
                return out                    # words the transcript never said: never a place
            try:
                place = self.locate(loc, self._scope(pack, row, key))      # the gazetteer decides
            except Exception:  # noqa: BLE001
                place = None
            if place is None or push_mod.scrub_label(place.label, self._who(pack, target)):
                return out
            place.confirmed = False                # the model's reading: never confirmed
            place.radius_m = max(place.radius_m, 150.0)
            place.basis = "; ".join(x for x in (place.basis, "place from inference") if x)
            up = self._set_place(pack, target, place, key, tid, at)
            if up is not None:
                self.infer.mark_used(res.record_id, place.label)
                out.append(up)
        return out

    def _end_window(self, pack, key: str, at: float, tid: str, win=None, ws=()) -> list[Event]:
        """ "Disregard" / "cancel" ends only the events of the current window's transmissions, or those it ties to by
        unit, place or category words (#492 re-check: never every event on the channel)."""
        out = []
        current = set(win.entries) - {tid} if win is not None else set()
        entry = win.entries.get(tid) if win is not None else None
        open_ = self._open_for(pack, None, key, at, self._merge_span(pack))
        named = [ev for ev in open_ if entry is not None and self._same_job(pack, ev, entry, list(ws))]
        # what it names (a unit, a place, the category's words); naming nothing, the current window's own events, but
        # never a safety call (shots, a barricade, a fire, a water rescue): that one is only "possibly cancelled"
        for ev in [] if named else [ev for ev in open_ if current & set(ev.traffic_ids) and ev.category in SAFETY]:
            ev.note, ev.updated_at = "possibly cancelled", self.clock()
            ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
            self.events.save(ev)
            self.sink.event(ev, "updated")
            out.append(ev)
        for ev in named or [ev for ev in open_ if current & set(ev.traffic_ids) and ev.category not in SAFETY]:
            ev.status, ev.updated_at = "ended", self.clock()
            ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
            self.events.save(ev)
            self.sink.event(ev, "ended")
            out.append(ev)
        self._consume(pack.name, key)
        return out

    def _deescalate(self, pack, row: dict, key: str, at: float, tid: str, text: str, ws: list[str], entry) -> list[Event]:
        """A category's "deescalate" phrase (rules.py; the weapons category: "took the knife away", "the gun is
        secured", "wasn't using it to threaten"): its open event on this source within merge_min (the span an open
        event takes a new mention: the follow-up often comes once units are there) is noted DEESCALATED, with this row
        in its traffic. Only an event this row ties to (_same_job: the category's own words, a unit, the talker, the
        place), and with more than one such event open, only the one at the place this row says (none said or none
        there: nothing is marked). Never a row that itself says the category ("took the knife away, now he has a
        gun"), never over a "possibly cancelled" note, never a push, an end or a new event. A later row that says the
        category again clears the note (_raise), and pushes again when it alone is over the threshold."""
        out = []
        for cat in pack.deescalated(text, ws):
            if any(r.category == cat and w > 0 for r, w in entry.hits):
                continue
            open_ = [e for e in self._open_for(pack, cat, key, at, self._merge_span(pack))
                     if self._same_job(pack, e, entry, ws, {r.category for r, w in entry.hits if w > 0})]
            if len(open_) > 1:
                if entry.place is None and self.locate is not None:
                    entry.place = self._where(pack, row, key, [text], list(entry.who))
                open_ = [e for e in open_ if entry.place is not None and e.precision != "none"
                         and same_place(entry.place, e.place())]
                if len(open_) != 1:
                    continue
            if not open_ or open_[0].note in (DEESCALATED, "possibly cancelled"):
                continue
            ev = open_[0]
            ev.note, ev.updated_at = DEESCALATED, self.clock()
            ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
            self.events.save(ev)
            self.sink.event(ev, "updated")
            out.append(ev)
        return out

    def _uncalm(self, pack, key: str, at: float, ws: list[str], entry, event_of: dict) -> list[Event]:
        """A row that says a category again, under the threshold by itself and not raised (the call-out's rows have
        left the window): the note DEESCALATED on its open event comes off (never a push: under the threshold)."""
        out = []
        mine = {r.category for r, w in entry.hits if w > 0}
        for cat in mine & set(pack.calm) - set(event_of):
            for ev in self._open_for(pack, cat, key, at, self._merge_span(pack)):
                if ev.note == DEESCALATED and self._same_job(pack, ev, entry, ws, mine):
                    ev.note, ev.updated_at = "", self.clock()
                    self.events.save(ev)
                    self.sink.event(ev, "updated")
                    out.append(ev)
        return out

    def _thr(self, pack, cat: str) -> float:
        cd = pack.category(cat)
        return cd["threshold"] if cd.get("threshold") is not None else pack.threshold

    @staticmethod
    def _alone(pack, entry, cat: str) -> float:
        """The category's score from this one row's hits (its rules and cues, noisy-OR; dampers aside)."""
        miss = 1.0
        for r, w in entry.hits:
            if w > 0 and r.category in (cat, rules_mod.WILD) and r.mode != "shadow":
                miss *= 1.0 - min(1.0, w)
        return round(1.0 - miss, 4)

    def _consume(self, pack_name: str, key: str) -> None:
        """What the window held is spent: a routine cue after "disregard" never adds it up again (#492 review, H2)."""
        win = self.windows.get((pack_name, key))
        if win is not None:
            win.entries = {t: e for t, e in win.entries.items() if e.suppressed}

    def dismissed(self, ev: Event) -> None:
        """A person dismissed it (sizeup/intel/admin.py): its window is spent, and mute_min keeps it from coming back."""
        with self._lock:
            self._consume(ev.pack, ev.source_key)

    def _after(self, pack, ev: Event, change: str, now: float, before: Event | None, moved: bool = False) -> None:
        self.sink.event(ev, "updated" if change == "reopened" else change)
        if change == "raised":
            self._count(now, "events", [pack.name])
        was = ev.suppressed
        d = self.policy.decide(ev, change, now, before, moved)
        if d.would_push:
            cat = pack.category(ev.category)
            doubtful = push_mod.doubtful_number(ev.label_full, self._texts(pack, ev.source_key, ev.traffic_ids))
            msg = push_mod.compose(ev, pack.label, cat, self.public_url, self.tz, doubtful=doubtful)
            # push_pii (Settings, on by default; admin decision 2026-11-17): no PII scrub, access is limited instead
            why = "" if self.policy.cfg.get("push_pii", True) else push_mod.scrub(msg, self._who(pack, ev))
            if why:
                self.log(f"intel: push of event {ev.id} withheld by the scrub ({why})")
                d = Decision(d.mode, False, "scrub", d.priority)
            else:
                d = Decision(d.mode, True, "", d.priority, d.repush, msg)
        sent = self.sink.push(ev, d)
        if d.would_push and sent is not False:    # counted only once the sink took it (#492 review, L1)
            ev.pushed_at, ev.push_count, ev.suppressed = now, ev.push_count + 1, ""
            self.policy.sent_now(ev, now)
            if ev.id in self.policy.held:
                self.policy.held.remove(ev.id)
            self.save_policy()
            self._count(now, "pushed", [pack.name])
        elif d.would_push:
            ev.suppressed = "unsent"
        elif d.suppressed and not ev.pushed_at:
            ev.suppressed = d.suppressed
        if ev.suppressed and not was:         # held, once per event (as events.counts() has it)
            self._count(now, "held", [pack.name])
        self.events.save(ev)

    def _who(self, pack, ev: Event) -> list[str]:
        out = []
        for (pname, _k), win in self.windows.items():
            if pname != pack.name:
                continue
            for tid in ev.traffic_ids:
                e = win.entries.get(tid)
                if e is not None:
                    out += list(e.who)
        return out

    def tick(self, now: float | None = None) -> list[Event]:
        """Events past their until: expired. A digest when due. Windows that have gone quiet: forgotten."""
        now = self.clock() if now is None else now
        out = []
        with self._lock:
            for ev in self.events.open_events():
                if ev.until and now >= ev.until:
                    ev.status, ev.updated_at = "expired", now
                    self.events.save(ev)
                    self.sink.event(ev, "expired")
                    out.append(ev)
            for k in [k for k, w in self.windows.items()
                      if not w.entries or max(e.at for e in w.entries.values()) < now - 2 * self._span(k[0])]:
                del self.windows[k]
            held = self.policy.digest_due(now) if self.enabled else []     # obeys enabled, push, quiet (M3)
            if held and hasattr(self.sink, "digest"):
                evs = [e for e in (self.events.get(i) for i in held) if e is not None]
                if evs:
                    self.sink.digest(evs)
                    self.save_policy()
        return out

    def _span(self, name: str) -> float:
        p = self.pack(name)
        return p.window_s if p is not None else 120.0


def push_rev(keys) -> int:
    """The revision of a set of push sources: a content hash of its sorted keys (sha256, the first 8 bytes, big-endian,
    masked to PUSH_REV_BITS), never 0 (that is "no engine / not computed"): the same set, the same rev, in any process."""
    h = int.from_bytes(hashlib.sha256("\n".join(sorted(keys)).encode()).digest()[:8], "big") & ((1 << PUSH_REV_BITS) - 1)
    return h or 1


def source_key(channel_key: str, kind: str = "") -> tuple[str, ...]:
    """The source keys (as rows carry them) a speech-to-text channel key may be: "tg:<n>" / "p25:<system>:<n>" ->
    sourcekeys.service_key's qualified key; "fm:<label>" / "air:<label>" as it is; a bare label -> sourcekeys.fm(label,
    kind) (App._row_source's: "air:" for kind "airband", else "fm:"), or with no kind both ("fm:" first). () for none."""
    from sizeup import sourcekeys
    key = str(channel_key or "")
    if not key:
        return ()
    if sourcekeys.parse(key) is not None:
        return (sourcekeys.service_key(key),)
    if key.startswith(("fm:", "air:")):
        return (key,)
    if kind:
        return (sourcekeys.fm(key, kind),)
    return (sourcekeys.fm(key), sourcekeys.fm(key, "airband"))


def can_push(pack) -> bool:
    """The pack could push an event: its mode is push, and one of its rules is on, at push, in one of its categories
    at push (a "*" cue alone never raises an event). As compiled, so with Settings' overrides (rules.load_packs)."""
    if pack.mode != "push":
        return False
    return any(r.enabled and r.mode == "push" and r.category in pack.categories
               and pack.category(r.category).get("mode", "push") == "push" for r in pack.rules)


def source_rows(talkgroups=(), channels=(), county: str = "") -> list[dict]:
    """Every known source as a row of the radio log carries it ({source, system, service, tier, dept}: what
    Pack.in_scope reads), for push_sources().

    talkgroups  TalkgroupPrefs.state()-shaped rows ({source, system, tg, group, service, service_auto, tier,
                department}): the station's list on the hub first, then the hub's own talkgroups files; a source given
                twice is the first one's
    channels    the configured FM and airband channels (config ChannelCfg: label, kind)

    Tagged as App._row_source and Comms.record tag a row: the service with no choice (the file's column, else the
    group's words; an FM channel's fire, an airband channel's air), the admins' radio_services choice over it
    (sourcekeys.overridden); a tier only for another P25 system's talkgroup."""
    from sizeup import sourcekeys
    county = county or sourcekeys.COUNTY
    out: dict[str, dict] = {}
    for r in talkgroups or ():
        if not isinstance(r, dict):
            continue
        system = str(r.get("system") or county)
        key = str(r.get("source") or "")
        if not key and r.get("tg") is not None:
            key = sourcekeys.p25(system, int(r["tg"]))
        if not key or key in out:
            continue
        auto = str(r.get("service_auto") or r.get("service") or "") or sourcekeys.service(str(r.get("group") or ""))
        out[key] = {"source": key, "system": system, "service": sourcekeys.overridden(key, auto),
                    "tier": "" if system == county else str(r.get("tier") or ""),
                    "dept": str(r.get("department") or r.get("dept") or "")}
    for c in channels or ():
        label, kind = str(getattr(c, "label", "") or ""), str(getattr(c, "kind", "") or "")
        key = sourcekeys.fm(label, kind)
        if not label or key in out:
            continue
        air = kind == "airband"
        out[key] = {"source": key, "system": "airband" if air else "analog",
                    "service": sourcekeys.overridden(key, sourcekeys.service(kind=kind or "traffic")), "tier": "",
                    "dept": ""}
    return list(out.values())


# The one engine of this process that reads the transcripts (the app sets it: App, HubApp), for push_sources() below
_live: IntelEngine | None = None


def set_live(engine: IntelEngine | None) -> None:
    global _live
    _live = engine


def push_sources() -> tuple[frozenset[str], int]:
    """IntelEngine.push_sources() of this process's engine, for code without the engine at hand (speech-to-text's
    queue). (frozenset(), 0) when none runs here: no engine, or a station forwarding to its hub (the hub's engine is
    the one that pushes; a station gets the set from it, see the module's doc)."""
    e = _live
    if e is None:
        return frozenset(), 0
    try:
        if not e.active():
            return frozenset(), 0
    except Exception:  # noqa: BLE001 - not known to be the one that reads: none
        return frozenset(), 0
    return e.push_sources()


def is_push_source(channel_key: str, kind: str = "") -> bool:
    """IntelEngine.is_push_source() of this process's engine; False when none runs here (as push_sources())."""
    keys, _rev = push_sources()
    return any(k in keys for k in source_key(channel_key, kind))


def rung(evidence: list, damp: list, thr: float, cues: list = ()) -> str:
    """The highest mode whose own rules reach thr: the category's evidence [(w, tid, rule)] at that mode or above
    (noisy-OR), with every cue, times every damper. A cue's own mode never moves the rung (as before the rung: a cue
    is evidence beside a category's rule, never a rule of its own). The lowest scored mode when none reaches thr (the
    caller checks WHAT against thr anyway)."""
    modes = sorted({r.mode for _w, _t, r in evidence}, key=mode_rank, reverse=True)
    for m in modes:
        miss = 1.0
        for w, _t, _r in cues:
            miss *= 1.0 - min(1.0, w)
        for w, _t, r in evidence:
            if mode_rank(r.mode) >= mode_rank(m):
                miss *= 1.0 - min(1.0, w)
        what = 1.0 - miss
        for w, _t, _r in damp:
            what *= 1.0 + max(-1.0, w)
        if round(what, 4) >= thr:
            return m
    return modes[-1] if modes else "off"


def _same(a: Event, b: Event) -> bool:
    keys = ("what_score", "level", "rules", "traffic_ids", "mode", "priority", "precision", "lat", "lon", "label_full",
            "confirmed", "status", "category", "near_incident", "suppressed")
    return all(getattr(a, k) == getattr(b, k) for k in keys)


class LiveSink:
    """The live effects: intel_hits in cad.sqlite, a line in the log, the push through the INTEL kind (the leader
    only, checked again right before sending)."""

    def __init__(self, engine: IntelEngine):
        self.engine = engine

    def hit(self, h: Hit) -> None:
        ev = self.engine.events
        if hasattr(ev, "hit"):
            ev.hit(h)

    def clear(self, tid: str, keep: set[str]) -> None:
        ev = self.engine.events
        if hasattr(ev, "clear_hits"):
            ev.clear_hits(tid, keep)

    def event(self, ev: Event, change: str) -> None:
        if mode_rank(ev.mode) >= mode_rank("log"):
            self.engine.log(f"intel: {change} event {ev.id} ({ev.pack} {ev.category}, {ev.level}, {ev.precision})")

    def push(self, ev: Event, d: Decision) -> bool:
        """True: queued for ntfy; False: not sent (no topic, the queue full, not the leader)."""
        n = self.engine.notifier
        if not d.would_push or d.message is None:
            return False
        if n is None or not self.engine.leader():
            return False
        if not getattr(n, "enabled", True):       # no intel topic in effect: held, and no Listen clip made for nothing
            self.engine.log(f"intel: event {ev.id}: no pre-alert topic in effect: not pushed")
            return False
        m = dict(d.message)
        actions = []
        lk = self.engine.listen
        if lk is not None:
            try:
                url = lk.link(f"intel:{ev.id}", [ev.source_key], ev.traffic_ids, ev.first_at, ev.last_at)
            except Exception as e:  # noqa: BLE001 - the push goes without its link
                self.engine.log(f"intel: listen link: {type(e).__name__}")
                url = None
            if url:
                m["body"] = f"{m['body']}\nListen: {url}"
                actions = [{"label": "Listen", "url": url}]
        if not n.send(m["title"], m["body"], m["priority"], m["tags"], m["click"], **({"actions": actions} if actions else {})):
            self.engine.log(f"intel: event {ev.id}: no pre-alert topic in effect (or the queue is full): not pushed")
            return False
        return True

    def digest(self, evs: list[Event]) -> None:
        n = self.engine.notifier
        if n is None or not self.engine.leader():
            return
        by: dict[str, int] = {}
        for e in evs:
            p = self.engine.pack(e.pack)
            label = (p.category(e.category).get("label") if p else "") or e.category
            by[label] = by.get(label, 0) + 1
        title = f"{len(evs)} more pre-alerts (over the hourly limit)"
        n.send(title, " · ".join(f"{k} {v}" for k, v in sorted(by.items())), 2, ["information_source"], "")


LOCATION_BEFORE = frozenset({"in", "at", "on"})              # "... in White Plains", "at Rye", "on Example Ave in Rye"
AREA_WORDS = frozenset({"area", "vicinity", "village", "town", "city"})   # "the area of Harrison"
NOT_LOCATION = frozenset({"units", "unit", "from", "to", "toward", "towards", "transporting", "transport", "transported",
                          "en", "route"})


def towns_said(texts) -> set[str]:
    """The county's towns these transmissions say a call IS in (lower case): a town after "in", "at" or "on" (or "the
    area / village of"), never after "to", "from", "transporting", "units from" (the hospital, the responding units),
    and a town before a street word is a street ("Harrison Ave")."""
    from sizeup.cad.sources import county_towns
    names = [k for k in county_towns() if k]
    out = set()
    for t in texts or ():
        ws = rules_mod.words(t)
        for i in range(len(ws)):
            prev = ws[i - 1] if i else ""
            prev2 = ws[i - 2] if i > 1 else ""
            located = (prev in LOCATION_BEFORE and prev2 not in NOT_LOCATION) or (prev == "of" and prev2 in AREA_WORDS)
            if not located:
                continue
            for k in names:
                n = len(k)
                if tuple(ws[i:i + n]) == k and not (i + n < len(ws) and ws[i + n] in STREET_WORDS):
                    out.add(" ".join(k))
    # "rye" inside "rye brook", "harrison" inside "west harrison": the longer name said is the one meant
    return {t for t in out if not any(o != t and f" {t} " in f" {o} " for o in out)}


def _m(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lat1 - lat2) * 110_574.0, (lon1 - lon2) * 111_320.0 * math.cos(math.radians(lat1)))


def same_place(a, b) -> bool:
    """Two places are one incident's: the same label, or within SAME_PLACE_M."""
    if a is None or b is None:
        return False
    if a.key and a.key == b.key:
        return True
    if None in (a.lat, a.lon, b.lat, b.lon):
        return False
    return _m(a.lat, a.lon, b.lat, b.lon) <= SAME_PLACE_M
