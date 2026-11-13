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
        self._pruned = 0.0
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
        if not self.threaded or not self.enabled:
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
        win.entries[tid] = Entry(at, hits, text, who, suppressed)
        win.prune(max(e.at for e in win.entries.values()), pack.window_s)
        changed: list[Event] = []
        if suppressed:
            changed += self._end_window(pack, key, at, tid)
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
            for ev in self._open_for(pack, None, key, at):
                if ev.category not in event_of and not ev.confirmed and self.locate is not None:
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
        cues = [(w, tid, r) for w, tid, r in best.values() if r.category == rules_mod.WILD and w > 0]
        damp = [(w, tid, r) for w, tid, r in best.values() if w < 0]
        out = {}
        for cat in pack.categories:
            pos = [(w, tid, r) for w, tid, r in best.values() if r.category == cat and w > 0]
            if not pos:
                continue
            miss = 1.0
            for w, _t, _r in pos + cues:
                miss *= 1.0 - min(1.0, w)
            what = 1.0 - miss
            for w, _t, _r in damp:
                what *= 1.0 + max(-1.0, w)
            top_mode = max((r.mode for _w, _t, r in pos), key=mode_rank)
            out[cat] = (round(what, 4), [(t, r, w) for w, t, r in pos + cues + damp], top_mode)
        return out

    def _open_for(self, pack, cat: str | None, key: str, at: float) -> list[Event]:
        return [e for e in self.events.open_events()
                if e.pack == pack.name and e.source_key == key and (cat is None or e.category == cat)
                and e.last_at >= at - pack.window_s]

    def _window_text(self, win: Window) -> str:
        return " . ".join(e.text for _t, e in win.ordered() if e.text and not e.suppressed)

    def _scope(self, pack, row: dict, key: str) -> dict:
        return {"source_key": key, "system": str(row.get("system") or ""), "service": str(row.get("service") or ""),
                "towns": pack.towns_for(key), "pack": pack.name}

    def _where(self, pack, row: dict, key: str, win: Window):
        if self.locate is None:
            return None
        try:
            return self.locate(self._window_text(win), self._scope(pack, row, key))
        except Exception as e:  # noqa: BLE001 - no place is still a pre-alert
            self.log(f"intel: locate: {type(e).__name__}")
            return None

    def _raise(self, pack, row, key, cat, cd, what, contrib, mode, win, at, tid) -> Event | None:
        now = self.clock()
        cur = self._open_for(pack, cat, key, at)
        ev = cur[0] if cur else None
        place = None
        if ev is None or not ev.confirmed:
            place = self._where(pack, row, key, win)
        if ev is None and place is not None and place.key:
            subject = f"{pack.name}:{cat}:{place.key}"
            ev = next((e for e in self.events.open_events() if e.subject == subject), None)
        before = copy.deepcopy(ev) if ev is not None else None
        tids = list(dict.fromkeys([*(t for t, _r, _w in contrib), tid]))      # the row that made it look again too
        rules = sorted({(r.id, r.v, round(float(w), 4)) for _t, r, w in contrib})
        if ev is None:
            ev = Event(domain=pack.domain, pack=pack.name, category=cat, source_key=key,
                       system=str(row.get("system") or ""), service=str(row.get("service") or ""),
                       dept=str(row.get("dept") or ""), first_at=at, status="open")
            change = "raised"
        else:
            change = "updated"
        ev.last_at = max(ev.last_at, at)
        ev.what_score = round(max(what, ev.what_score if change == "updated" else 0.0), 4)
        ev.level = level(ev.what_score)
        ev.rules = [list(x) for x in sorted({tuple(x) for x in ev.rules} | set(rules))] if change == "updated" else [list(x) for x in rules]
        ev.ruleset_hash = self._hash
        ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, *tids]))
        ev.mode = mode if change == "raised" else max(ev.mode, mode, key=mode_rank)
        ev.priority = max(int(cd.get("priority", 3)), ev.priority if change == "updated" else 0)
        ev.until = max(ev.until, ev.last_at + float(cd.get("ttl_min", 45)) * 60)
        if place is not None and (ev.precision == "none" or place.confirmed or place.where_score > ev.where_score):
            ev.set_place(place)
        ev.subject = f"{pack.name}:{cat}:{ev.place().key or key}"
        if before is not None and _same(before, ev):
            return None                       # idempotent: the same row again changes nothing
        ev.updated_at = now
        self.events.save(ev)
        self._after(pack, ev, change, now, before)
        return ev

    def _relocate(self, pack, ev: Event, row, key, win, tid, at) -> Event | None:
        place = self._where(pack, row, key, win)
        if place is None or not (ev.precision == "none" or place.confirmed or place.where_score > ev.where_score):
            return None
        before = copy.deepcopy(ev)
        ev.set_place(place)
        ev.last_at = max(ev.last_at, at)
        ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
        ev.subject = f"{pack.name}:{ev.category}:{place.key or key}"
        if _same(before, ev):
            return None
        ev.updated_at = self.clock()
        self.events.save(ev)
        self._after(pack, ev, "updated", ev.updated_at, before)
        return ev

    def _end_window(self, pack, key: str, at: float, tid: str) -> list[Event]:
        out = []
        for ev in self._open_for(pack, None, key, at):
            ev.status, ev.updated_at = "ended", self.clock()
            ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
            self.events.save(ev)
            self.sink.event(ev, "ended")
            out.append(ev)
        return out

    def _after(self, pack, ev: Event, change: str, now: float, before: Event | None) -> None:
        self.sink.event(ev, change)
        d = self.policy.decide(ev, change, now, before)
        if d.would_push:
            cat = pack.category(ev.category)
            msg = push_mod.compose(ev, pack.label, cat, self.public_url, self.tz)
            why = push_mod.scrub(msg, self._who(pack, ev))
            if why:
                self.log(f"intel: push of event {ev.id} withheld by the scrub ({why})")
                d = Decision(d.mode, False, "scrub", d.priority)
            else:
                d = Decision(d.mode, True, "", d.priority, d.repush, msg)
        if d.would_push:
            ev.pushed_at, ev.push_count, ev.suppressed = now, ev.push_count + 1, ""
            self.policy.sent_now(ev, now)
            if ev.id in self.policy.held:
                self.policy.held.remove(ev.id)
        elif d.suppressed and not ev.pushed_at:
            ev.suppressed = d.suppressed
        self.events.save(ev)
        self.sink.push(ev, d)
        if d.would_push:
            self.save_policy()

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
            held = self.policy.digest_due(now)
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

    def push(self, ev: Event, d: Decision) -> None:
        n = self.engine.notifier
        if not d.would_push or d.message is None or n is None or not self.engine.leader():
            return
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
