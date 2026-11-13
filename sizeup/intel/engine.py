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

The sink (no side effect happens in the engine itself):

    sink.hit(Hit)                 every hit: shadow, below threshold and suppressed-by-mode too
    sink.event(Event, change)     raised | updated | ended | expired
    sink.push(Event, Decision)    the policy's verdict; Decision.message is the composed, scrubbed push when would_push
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
  5. mode = the lowest of the pack's, the category's and the best contributing rule's (off: no event; shadow: an event
     only in the store, never shown or pushed); policy.decide() says whether to push, push.compose() + scrub() what.
Intel never writes incidents, radio_links, incoming or alarms.
"""

from __future__ import annotations

import copy
import json
import math
import re
import queue
import threading
import time
from dataclasses import dataclass, field

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
SAME_PLACE_M = 150.0                   # two places this close are the same incident's
STOP_WORDS = {"from", "with", "that", "this", "the", "and", "into", "front", "near", "area", "call", "calls", "caller",
              "report", "reported", "respond", "units", "unit", "male", "female", "person", "subject", "someone"}
QUEUE_MAX = 5000
SEEN_MAX = 20000                       # traffic ids remembered with their text (the same text again: skipped)
ROW_SQL = ("SELECT id, start, end, source, system, service, dept, tier, talkgroup, channel, talker, rig, radio_id, text, "
           "alt_text, model FROM radio_traffic WHERE id = ?")
SECTIONS = ("intel", "intel_packs", "intel_rules")


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

    def prune(self, newest: float, span: float) -> None:
        for tid in [t for t, e in self.entries.items() if e.at < newest - span]:
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
                    self.log(f"intel: Settings' rule changes not applied ({e}): the pack files alone")
                    self.packs = rules_mod.load_packs(None, {})
            self._hash = rules_mod.ruleset_hash(self.packs)
            self._catwords = {}

    def ruleset_hash(self) -> str:
        return self._hash

    @property
    def enabled(self) -> bool:
        return bool(self.policy.cfg.get("enabled", True))

    def pack(self, name: str):
        return next((p for p in self.packs if p.name == name), None)

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

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tid = self._q.get(timeout=TICK_S)
            except queue.Empty:
                tid = None
            try:
                if tid is not None:
                    self._one(tid)
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
        if now - self._ticked >= TICK_S and self.leader():
            self._ticked = now
            self.tick(now)
            if self.listen is not None:
                self.listen.refresh(now)      # later transmissions into the links already pushed
            if now - self._pruned >= PRUNE_S and hasattr(self.events, "prune"):
                self._pruned = now
                self.events.prune(now)

    def _restore(self) -> None:
        if self.store is None:
            return
        try:
            self.policy.restore(json.loads(self.store.meta_get("intel_policy") or "{}"))
        except (ValueError, TypeError):
            pass

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
        if start and self.clock() - start > float(self.policy.cfg.get("max_age_min", 15)) * 60:
            self.stale += 1
            return []
        sig = hash((text, str(row.get("alt_text") or "")))
        if self._seen.get(tid) == sig:
            return []
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
                hits or any(not e.confirmed or pack.category(e.category).get("moves")
                            or (e.category in PEOPLE and MOVE_WORDS.search(text))
                            for e in self._open_for(pack, None, key, at, self._merge_span(pack)))):
            entry.place = self._where(pack, row, key, [text], list(who))
        win.entries[tid] = entry
        win.prune(max(e.at for e in win.entries.values()), pack.window_s)
        changed: list[Event] = []
        if suppressed:
            changed += self._end_window(pack, key, at, tid, win, ws)
        cats = self._scores(pack, win)
        mine = {r.category for r, _w in hits}
        event_of: dict[str, Event] = {}
        for cat, (what, contrib, best_mode) in cats.items():
            cd = pack.category(cat)
            thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
            if what < thr:
                continue
            if not (mine & {cat, rules_mod.WILD, "routine"}):
                continue                      # a candidate this row says nothing about: left as it is
            mode = lowest(pack.mode, cd.get("mode", "push"), best_mode)
            if mode == "off":
                continue
            ev = self._raise(pack, row, key, cat, cd, what, contrib, mode, win, at, tid)
            if ev is not None:
                event_of[cat] = ev
                changed.append(ev)
        if not hits and not suppressed:       # a read-back with no rule words: may still place an open event
            for ev in self._open_for(pack, None, key, at, self._merge_span(pack)):
                moving = self._moving(pack, ev, win.entries[tid])
                if (ev.category not in event_of and (not ev.confirmed or moving) and self.locate is not None
                        and self._same_job(pack, ev, win.entries[tid], ws, mine)):
                    up = self._relocate(pack, ev, row, key, win, tid, at)
                    if up is not None:
                        changed.append(up)
        keep = {r.id for r, _w in hits}
        if known and hasattr(self.sink, "clear"):
            self.sink.clear(tid, keep)
        for r, w in hits:
            ev = event_of.get(r.category)
            if ev is None and r.category in (rules_mod.WILD, "routine") and len(event_of) == 1:
                ev = next(iter(event_of.values()))
            self.sink.hit(Hit(tid, r.id, r.v, round(float(w), 4), r.category, pack.name, at, ev.id if ev else None))
        return changed

    def _scores(self, pack, win: Window) -> dict:
        """{category: (WHAT, [(tid, rule, w)], best contributing rule's mode)} over the window."""
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
            top_mode = max((r.mode for _w, _t, r in pos), key=mode_rank)
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
        return {"source_key": key, "system": str(row.get("system") or ""), "service": str(row.get("service") or ""),
                "towns": pack.towns_for(key), "pack": pack.name}

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
        return place

    def _raise(self, pack, row, key, cat, cd, what, contrib, mode, win, at, tid) -> Event | None:
        now = self.clock()
        entry = win.entries[tid]
        speaks = any(r.category == cat and w > 0 for r, w in entry.hits)     # the row itself says this category
        rp = entry.place if speaks else None
        fresh = bool(NEW_CALL.search(entry.text))
        ev, moved, reopened = None, False, False
        for e in self._open_for(pack, cat, key, at, self._merge_span(pack)):
            ep = e.place() if e.precision != "none" else None
            if rp is not None and ep is not None and same_place(rp, ep):
                ev = e                        # the same place again ("another caller ...": more of the same)
            elif fresh:
                continue                      # "new call": another place, or none said: a new incident
            elif rp is None and (speaks or self._same_job(pack, e, entry, rules_mod.words(entry.text), {cat})):
                ev = e                        # no place said ("still smoke showing"): the open one (#492 re-check)
            elif rp is not None and ep is None and e.last_at >= at - pack.window_s:
                ev = e                        # the place, right after a call-out that had none
            elif (rp is not None and ep is not None and self._moving(pack, e, entry)
                  and (cd.get("moves") or self._same_job(pack, e, entry, rules_mod.words(entry.text), {cat}))):
                ev, moved = e, True           # a moving incident ("now at", a pursuit): its place moves
            if ev is not None:
                break
        if ev is not None and ev.note == "possibly cancelled" and speaks:
            reopened = True                   # a nameless disregard, then the call again: it stands, pushed again
            ev.note = "re-opened"
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
        if before is not None and _same(before, ev):
            return None                       # idempotent: the same row again changes nothing
        ev.updated_at = now
        self.events.save(ev)
        self._after(pack, ev, change, now, before, moved=moved and before is not None and before.precision != "none"
                    and not same_place(before.place(), ev.place()))
        return ev

    def _relocate(self, pack, ev: Event, row, key, win, tid, at) -> Event | None:
        entry = win.entries[tid]
        place = entry.place                            # its own words only (a tied row: _same_job said so)
        move = (place is not None and ev.precision != "none" and not same_place(place, ev.place())
                and self._moving(pack, ev, entry))
        if place is None or not (move or ev.precision == "none" or place.confirmed or place.where_score > ev.where_score):
            return None
        before = copy.deepcopy(ev)
        ev.set_place(place)
        if move:
            ev.note = "moved"
        ev.last_at = max(ev.last_at, at)
        ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
        ev.subject = f"{pack.name}:{ev.category}:{place.key or key}"
        if _same(before, ev):
            return None
        ev.updated_at = self.clock()
        self.events.save(ev)
        self._after(pack, ev, "updated", ev.updated_at, before, moved=move)
        return ev

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
        d = self.policy.decide(ev, change, now, before, moved)
        if d.would_push:
            cat = pack.category(ev.category)
            doubtful = push_mod.doubtful_number(ev.label_full, self._texts(pack, ev.source_key, ev.traffic_ids))
            msg = push_mod.compose(ev, pack.label, cat, self.public_url, self.tz, doubtful=doubtful)
            why = push_mod.scrub(msg, self._who(pack, ev))
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
        elif d.would_push:
            ev.suppressed = "unsent"
        elif d.suppressed and not ev.pushed_at:
            ev.suppressed = d.suppressed
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


def _same(a: Event, b: Event) -> bool:
    keys = ("what_score", "level", "rules", "traffic_ids", "mode", "priority", "precision", "lat", "lon", "label_full",
            "confirmed", "status", "category")
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
