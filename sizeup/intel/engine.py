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
                          channel, talker, rig, radio_id, text, alt_text, model, status, late}: synchronous
    engine.consider(tid)  live: queue the id (never waits on rules or the gazetteer); the thread loads the row (its
                          corrected text when there is one, #901's corr_text; its #747 late flag), feed()s it
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

Talkers (sizeup/intel/talkers.py; engine.talkers.radios: the CAD's Settings > Radios > Radio IDs, #844): another
system's radio ID is looked up once per registry revision (cached, in memory) as its label, agency, role and whether it
is a dispatch console. A console's transmission is the dispatcher's (never a tie by talker alone); beside a category's
hit it may count as the pack's dispatch cue ("<prefix>dispatch_cue", its weight, once) without the cue words: the
pack's "console_cue" (rules.py) on; shadow, the default, only records a hit "<cue id>.console" of weight 0 where it
would have. The department's town (rules.py "agency_towns", else the source's towns by name or initials) is the town
prior of step 4 (_town_prior: the row's own talker's town; else the window's consoles' when they all say one town and
one of them is on this job; else none). The push says who it was heard from by the label (push.compose "heard"). No
registry, or an unlabelled radio: as before.

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
  4. WHERE: the window's words through locate() (only while not confirmed); on a source shared by several towns, the
     talker's department's town first (_town_prior, below), a preference that never confirms (place.py);
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
     A category with "fire_related": true (rules.py; the police pack's fire_report, fire_call, hazmat, alarm) is checked
     for the FD already being there before it pushes (_fire; admin, 2026-11-17: a pre-alert is a jump on a call, and
     PD's dispatch of a fire the FD is already working is not one): our fire incident (the CAD's own and the incident
     sources', the county feed's included: fire_incidents; a type "fd_present" "types" allows for the category) at
     its place (the same address or block, or a confirmed address or intersection within fd_radius_m), open and made
     up to FD_OPEN_S before it, or closed within fd_window_min; with no place, an open one whose street the words name
     in its town. Linked (Event.near_incident) and Event.fd set either way:
       fd_on_scene    a unit on scene: held at log, unless the words are a safety change ("fd_present" "safety": a
                      weapon, violence, an explosion or collapse, a member down): then an FYI at safety_priority
       fd_dispatched  units assigned or en route, not cleared (or none known yet on an open incident made at most
                      FD_PAGED_S before: toned out): held at log, unless the words add something
                      ("add_info": people inside or trapped, injuries, spread, hazmat): an FYI at fyi_priority (2);
                      a safety change: at safety_priority
       fd_there       none of ours, but the words say the FD is there ("the fire department is already working it":
                      another department's call, not in our feed): as fd_dispatched
     The FD cleared (the incident closed, every unit cleared or in service, the feed's event closed): a pre-alert as
     before (a rekindle), its body saying "FD here HH:MM". An FYI's title starts "FYI" and its body ends "FD on scene ·
     #<number>". An event pushed before the FD was known is linked only, never lowered; one pushed as an FYI keeps its
     FYI priority. A held call whose FD has cleared since (a rekindle merged into it) is let go as a pre-alert, "FD here
     HH:MM"; one whose incident is gone (dismissed, no longer at the place) more than RELEASE_S after the call stays at
     log ("fd_late"). An event of another category is never checked.
     When a hold ends (the place read back at one of our streets), the event gets back its rules' mode.
     A category with "place": true (rules.py; any pack) is held the same way while it has no place a crew can drive to
     (PLACED) in the source's towns, else the pack's area: "unplaced", or "out_of_area" for a place elsewhere. Only its
     own words or a read-back of the held call place it (_may_place); placed more than RELEASE_S after the call-out it
     stays at log ("placed_late"). A category with "defer_to" is never raised beside that category (_deferred), and an
     event of that category takes its open event over: one pre-alert per call. The tick never lets a held call go: an
     unplaced one stays at log until it expires (#640: a burning smell with no address is no pre-alert).
     A category with "highway": true (data/intel/highway.json: a crash heard first on another agency's channel) is held
     by its own gate instead (_hw_hold, sizeup/intel/highway.py): its place is the highway's (an exit, a mile marker,
     an interchange, the route alone: engine.highway, the place search's markers and our district's shapes), held at
     log while it isn't within the pack's highway.buffer_m of our district ("out_of_area") or isn't known
     ("unplaced"), or while our own call is already at it ("near_incident"). Another source's report of the same crash
     (the same route, within merge_m and merge_min) joins its event (_hw_same: one push). Our call made after the push
     (within link_min, link_m) links it on the tick: kept at map, never pushed again (_hw_dispatched). While such an
     event is live (pushed; never one held) its sources are promoted for promote_min (promoted_since():
     speech-to-text's operating-channel promotion, App.promoted_keys, so the follow-ups aren't shed as context).
     A push can only be as early as the transcript: a row is read when its text lands (consider), so speech-to-text's
     queue (sizeup/stt.py: another system's "text" talkgroups wait in the LOW class) is the delay before a push, never
     the engine. Air-time order (FIFO): speech-to-text runs on several engines, so a source's transcripts can arrive
     out of air-time order, and a row's text can change later (a correction, an "unclear" re-ask, a catch-up). A row
     is read with the rows that AIRED within window_s of it (Window.around; a source's rows are kept max_age_min +
     window_s by air time, and as long again after they were read), whenever it arrives; a new event's first_at is its
     first row's air time. A row read over its pack's late_s after its clip ended, older than max_age_min, or #747's
     (radio_traffic.late) is LATE (Entry.late): context and corroboration, never a push's origin, never asked of the
     model. An event is pushed a first time only while its newest row read in time ended within late_s, and those rows
     reach the threshold on their own (_fresh); else it is recorded at log ("late"). A late row, or a
     re-text of a row already in the event, never pushes it again. A re-text replaces the row's hits in place; one
     that no longer says an event's category scores that event again without it (_unsupport). A row whose text
     goes empty or "unclear" is withdrawn (_withdraw): its hits, its window entry and its model context go, and an
     event that rested on it alone is noted "source withdrawn" (ended if never pushed; a push is never undone). A
     disregard, a de-escalation or a re-open is undone only by a row that aired after it.
     The address lookup (step 4) runs only for a row that could carry a category to the threshold with the window's
     hits (a cue or a damper alone never makes an event, so it never pays for the gazetteer), and, for a pack with an
     area, only in those towns.
  7. last, the local model behind the rules (backing.ModelBacking, engine.backing): a row of the police pack the rules
     raised nothing with may be asked about off this thread; its answer comes back through _raise like a rule's.
Intel never writes incidents, radio_links, incoming or alarms.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import re
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sizeup import stt_quality
from sizeup.intel import counters as counters_mod
from sizeup.intel import policy as policy_mod
from sizeup.intel import push as push_mod
from sizeup.intel import rules as rules_mod
from sizeup.intel import talkers as talkers_mod
from sizeup.intel.model import Decision, Event, Hit, lowest, mode_rank
from sizeup.intel.policy import Policy, level
from sizeup.radio.p25 import UNKNOWN_SRC

TICK_S = 15.0
PRUNE_S = 3600.0
PRUNE_FIRST_S = 3 * 3600.0             # the first prune this long after start (never in the startup's busy minutes)
UNIT_MENTION = re.compile(r"\b(car|unit|adam|boy|charlie|david|sector|post|rmp|patrol)\s+(\d{1,4}[a-z]?)\b", re.I)
# a new incident: only these ("another caller", "another one", "additional callers" are MORE of the same one)
NEW_CALL = re.compile(r"\b(new call|second call|another call|different (call|location|address))\b", re.I)
# a moving incident's new place ("now at", "last seen", "fled", "heading", "continuing", "still")
MOVE_WORDS = re.compile(r"\b(now at|now on|last seen|fled|fleeing|heading|continuing|still|turned onto|turning onto)\b", re.I)
# a dispatcher / HQ talker (they voice every job); never a unit's alias such as "RYE PD CAR 3"
DISPATCH_WORDS = re.compile(r"\bdisp\b|dispatch|\bhq\b|headquarters|\bcontrol\b|\bctrl\b|console|\bdesk\b", re.I)
PEOPLE = {"weapons", "edp", "dispute"}   # incidents about a person, who may move ("last seen at")
SAFETY = {"weapons", "barricade", "water_rescue", "fire_report"}   # never ended by a disregard that names nothing; nor
                                                                    # is any category its pack marks "safety" (_safe)
OTHER_JOB = re.compile(r"\b(other|another|different|second) (job|call|incident|scene|assignment)\b", re.I)
DEESCALATED = "de-escalated"           # the note a category's "deescalate" phrase leaves on its open event
SAME_PLACE_M = 150.0                   # two places this close are the same incident's
NEAR_INCIDENT_M = 150.0                # an event this close to one of our open incidents is that incident
INCIDENT_BEFORE_S = 6 * 3600.0         # ... an incident opened at most this long before the event's first transmission
INCIDENT_AFTER_S = 30 * 60.0           # ... or at most this long after its last (the radio heard first, then our call)
FD_HELD = ("fd_on_scene", "fd_dispatched", "fd_there")   # a fire-related event held for the FD being there (_fire)
FD_FYI = ("on_scene", "dispatched", "there")              # Event.fd of an event the FD is at: a push of it is an FYI
FD_RANK = {"on_scene": 3, "dispatched": 2, "cleared": 1}   # which of two incidents at the place is the one (_fire_incident)
FD_PAGED_S = 30 * 60.0                 # an open incident with no unit known yet, made at most this long before the
                                       # event, is the FD already toned out (a page's own card): as dispatched
FD_OPEN_S = 12 * 3600.0                # an open incident counts this long after it was made (a long fire); a closed one
                                       # only when closed within fd_window_min (a rekindle after: a pre-alert, linked)
PLACED = ("address", "intersection", "block", "landmark")     # a place a crew can drive to ("place": true categories)
RELEASE_S = 300.0                      # an "unplaced" hold: a read-back this soon after the call-out places it, and a
                                       # place found later than this updates the event at log ("placed_late"), unpushed
LATE_S = RELEASE_S                     # air-time order (FIFO): the default of a pack's late_s (rules.Pack.late_s; the
                                       # police pack's 300 s is speech-to-text's HIGH shed, highway's 600 s its CONTEXT
                                       # one): a row read longer than that after its clip ENDED is late (Entry.late)
WITHDRAWN = "source withdrawn"         # the note on an event whose only support was a row withdrawn since (_unsupport)
IGNORE_S = 24 * 3600.0                 # a row older than this (or max_age_min if longer) is never read at all
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
ROW_SQL = ("SELECT id, start, end, source, system, service, dept, tier, talkgroup, channel, talker, rig, radio_id, {text}, "
           "alt_text, model, status{extra} FROM radio_traffic WHERE id = ?")
# the post-ASR correction (#901: radio_traffic.corr_text, made before the row commits) is the text intel scores, never
# the raw one beside it; a database without the column (before #901) reads the text as transcribed
CORR_TEXT = "COALESCE(NULLIF(corr_text, ''), text) AS text"


# read where the database has them (PRAGMA, _row_sql): the #747 late flag (#917), and what made the text for the late
# rows' causes (late_cause: the engine #925 records, its catch-up flag, the catch-up's re-run note)
EXTRA_COLUMNS = ("late", "rerun_result", "stt_engine", "stt_catchup")
LATE_CAUSES = (("aws", "AWS"), ("gpu", "GPU"), ("station_pool", "station pool"), ("station", "station"),
               ("catchup", "catch-up"), ("outage", "outage"), ("other", "other"))


def late_cause(row: dict) -> str:
    """Why a push source's text reached intel late: "outage" (#747: a call from while sizeup was down), "catchup"
    (a shed clip caught up: #925's stt_catchup, else the re-run note), else the engine that made it (#925's
    stt_engine, else from the model label: "aws-transcribe" AWS, "(GPU " the GPU worker, "(station " the station's
    pool, any other the station's whisper; a late AWS answer kept as alt_model is never this row's text)."""
    if row.get("late"):
        return "outage"
    if row.get("stt_catchup") or ("stt_catchup" not in row and "catch-up" in str(row.get("rerun_result") or "")):
        return "catchup"
    eng = str(row.get("stt_engine") or "")
    if eng in {c for c, _w in LATE_CAUSES}:
        return eng
    m = str(row.get("model") or "")
    if m == "aws-transcribe":
        return "aws"
    if "(GPU " in m:
        return "gpu"
    if "(station " in m:
        return "station_pool"
    return "station" if m and m != "gpu-worker" else "other"


def late_row(snap: dict, now: float, packs=()) -> dict:
    """Settings > Station > Health's Intel line for the push sources' late text (counters' late_* over 24 h). packs:
    the ones that can push (their late s in the hover). The row's label says what it is, so the text is the numbers."""
    h = snap["h24"]
    n, lost = h.get("late", 0), h.get("late_lost", 0)
    by = ", ".join(f"{w} {h.get('late_' + c, 0)}" for c, w in LATE_CAUSES if h.get("late_" + c, 0))
    text = f"24 h: {n}" + (f" ({by})" if by else "") + (f" · would have pushed: {lost}" if lost else "")
    limits = ", ".join(f"{p.label} {p.late_s / 60:g} min" for p in packs)
    title = ("Transcripts of the sources intel pushes from that reached it later than their pack's late s after the "
             f"clip ended ({limits}), or recorded while the hub was down: read as context and kept at log, never "
             "pushed. By what made the text: the engine, a catch-up of a shed clip, an outage. Would have pushed: on "
             "time, the row would have raised a new pre-alert, so one was lost. To cut it: raise the police AWS "
             "budget, keep the PD channels on Auto · priority.")
    return {"pack": "late", "label": "Late PD text", "on": True, "level": "warn" if lost else "ok", "text": text,
            "title": title, "late": n, "lost": lost}


def text_sql(columns) -> str:
    """The text column as intel reads it, given radio_traffic's columns: the corrected text when there is one."""
    return CORR_TEXT if "corr_text" in set(columns) else "text"

SECTIONS = ("intel", "intel_packs", "intel_rules")
SUMMARY_S = 3600.0                     # one "intel: considered N, in scope M, matched K, events E" line this often
COUNTERS_META = "intel_counters"       # the counters' hours, kept with that line (never written at startup)
LATE_META = "intel_late_ids"           # ... and the rows counted late (counters.late_state), under their own key
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
    ties: frozenset = frozenset()      # who keyed up, as a tie by talker reads it (_talker_ties): radio IDs, not text
    place: object = None               # the place in its own words (model.Place), or None
    late: bool = False                 # logged late (#747, row["late"]; older than max_age_min) or read over the
                                       # pack's late_s after its clip ended: context and a record, never a push's origin
    read: float = 0.0                  # when it was read (the clock): kept as context a while after, whenever it aired
    end: float = 0.0                   # when its clip ended (lateness is measured from it)
    label: str = ""                    # its radio as Settings > Radios names it (talkers.py), "" none
    town: str = ""                     # ... that department's town, one of the source's ("" not known)


@dataclass
class Window:
    entries: dict = field(default_factory=dict)      # tid -> Entry

    def prune(self, newest: float, span: float, keep: str = "", now: float | None = None) -> None:
        """Entries older than span before the newest go; never `keep` (the row being read: a transmission transcribed
        late, after newer ones of its source, is still judged, with what the window holds, instead of dropped).
        The engine keeps the source's rows for max_age_min + window_s (Window.around picks a row's own neighbours), so
        a row transcribed late still finds, and is found by, the rows that aired around it; with now, a row read within
        span before it is kept too (a burst of late rows, #747: each the others' context)."""
        for tid in [t for t, e in self.entries.items()
                    if e.at < newest - span and t != keep and (now is None or e.read < now - span)]:
            del self.entries[tid]

    def around(self, at: float, span: float) -> "Window":
        """The rows that aired within span of `at` (either side), in AIR-TIME order semantics: a late transcript is
        judged with its own neighbours, never with whatever arrived last. The same Entry objects (a view)."""
        return Window({t: e for t, e in self.entries.items() if abs(e.at - at) <= span})

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


def _log_id(key) -> str:
    """A pack name or a rule id as a log line may carry it ("pd.shots"); anything else a Settings key could hold
    (free text) is never logged."""
    k = str(key)
    return k if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", k) else "(a key that is no id)"


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
        self.backing = None                   # backing.ModelBacking: the local model behind the police rules (rows they left)
        # who keyed up (talkers.py): the CAD sets talkers.radios to its radio registry (Settings > Radios > Radio IDs);
        # unset, no talker is named and every row reads as before
        self.talkers = talkers_mod.Talkers()
        # incidents(now) -> [{id, lat, lon, label, created_at, closed_at}]: our own open and recent incidents (the CAD
        # sets it; replay may), for near_incident. None: no incidents, nothing linked.
        self.incidents = None
        # fire_incidents(opened_since, closed_since) -> [{id, number, type, lat, lon, label, address, created_at,
        # closed_at, fd, fd_at}]: our incidents (the CAD's own and the incident sources', the county feed's included)
        # made since opened_since and open, or closed since closed_since; fd "on_scene" | "dispatched" | "cleared" | ""
        # (no unit known yet), fd_at when it cleared. For a fire-related event (_fire). None: never checked.
        self.fire_incidents = None
        # highway: a highway.Corridor, or a function returning one (the app's: the place search's markers and our
        # district's shapes), for a "highway": true category (_hw_*). None: such an event is never placed (held at log)
        self.highway = None
        # on_promote(): a source was promoted (a crash pre-alert live on it, promoted_since()): the app tells
        # speech-to-text at once (App.promoted_changed); called on a thread of its own, never under the engine's lock
        self.on_promote = None
        self._promo: dict[str, tuple[float, float]] = {}      # source key -> (since, until): promoted_since()
        self._promo_first: dict = {}                           # event id -> its first promotion (promote_max_min)
        self._promo_lock = threading.Lock()
        self._numbers: dict = {}              # incident id -> (its number, when the FD cleared), as fire_incidents
                                              # last gave them (the push, the console: fd_shown)
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
        self._row_q: str | None = None        # ROW_SQL for this database (_row_sql)
        self._late_now: tuple | None = None   # feed(): the row read late, its cause (late_row)
        self._lost_now = False                # _feed_pack/_raise: the late row would have raised a pre-alert on time
        self._again = False                   # feed(): this row was read before (a re-text): never counted late again
        self._unkept: set[str] = set()        # _summary: the meta keys a save skipped for their size (said once)
        self._as_fresh = False                # _on_time_push: _fresh says yes
        self.unclear = 0                      # unclear transcripts (a loop, noise: sizeup/stt_quality.py): skipped
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
                    # the id and the exception's type only: its text quotes the admin's phrases and town names
                    self.log(f"intel: Settings' {sec}/{_log_id(key)} not applied ({type(e).__name__})")
        try:
            return rules_mod.load_packs(None, good)
        except (ValueError, OSError) as e:
            self.log(f"intel: Settings' rule changes not applied ({type(err).__name__}; {type(e).__name__}): "
                     "the pack files alone")
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
        packs = [p for p in self.packs if can_push(p) and p.stt_high]     # ("stt_high": false: pushes, never raises STT)
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
        if self.store is not None:
            try:
                self._row_sql()               # radio_traffic's columns (corr_text, late) looked up once, at start
            except Exception as e:  # noqa: BLE001 - looked up again at the first row
                self.log(f"intel: radio_traffic columns: {type(e).__name__}")
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
        if self.store is None:
            return
        try:
            row = self.store.one(self._row_sql(), (tid,))
        except sqlite3.OperationalError:
            self._row_q = None                # a column gone since it was looked up: looked up again, once
            row = self.store.one(self._row_sql(), (tid,))
        if row:
            self.feed(row)

    def _row_sql(self) -> str:
        """ROW_SQL for this database: its columns looked up once (the corrected text, and a "late" flag, where the
        columns exist)."""
        if self._row_q is None:
            cols = [r["name"] for r in self.store.q("PRAGMA table_info(radio_traffic)")]
            self._row_q = ROW_SQL.format(text=text_sql(cols), extra="".join(f", {c}" for c in EXTRA_COLUMNS if c in cols))
        return self._row_q

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
            late = self.store.meta_get(LATE_META) or "{}"
            if len(late) <= counters_mod.LATE_STATE_MAX:
                counters_mod.restore_late(self.counters, json.loads(late), now)
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
                raw = json.dumps(self.counters.state(now, [p.name for p in self.packs]), separators=(",", ":"))
                late = json.dumps(counters_mod.late_state(self.counters, now), separators=(",", ":"))
                for key, val, cap in ((COUNTERS_META, raw, counters_mod.STATE_MAX),
                                      (LATE_META, late, counters_mod.LATE_STATE_MAX)):
                    if len(val) <= cap:
                        self.store.meta_put(key, val)
                    elif key not in self._unkept:          # never silent; one line a kind (type and size only)
                        self._unkept.add(key)
                        self.log(f"intel: {key} not kept: {len(val)} characters over {cap}")
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
        pushing = [p for p in packs if can_push(p)]
        late = late_row(snap["all"], now, pushing) if self.enabled and pushing else None     # nothing to lose: hidden
        return {"started": snap["started"], "all": snap["all"], "packs": rows, "late": late}

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
        max_age = float(self.policy.cfg.get("max_age_min", 15)) * 60
        if start and now - start > max(IGNORE_S, max_age):
            self.stale += 1                   # a backfill of days: never read (it can't flood the engine)
            if scoped:
                self._count(now, "too_old", scoped)
            return []
        # an unclear transcript (sizeup/stt_quality.py: a repetition loop, more words than the clip holds, whisper's
        # stock phrase) is never read: the CAD of record stores it with no text, and words that reach here some other
        # way (a replay, an old row) are checked again; an unclear alternative is left out alike. A row read before
        # whose text is now unclear or empty (#890's re-ask) is withdrawn, whatever its age
        secs = max(0.0, float(row.get("end") or 0.0) - start) if start else None
        unclear = str(row.get("status") or "") == stt_quality.UNCLEAR or bool(text and stt_quality.assess(text, secs))
        if unclear or not text.strip():
            if unclear:
                self.unclear += 1
            with self._lock:
                return self._withdraw(row, tid) if self._read_before(tid) else []
        if row.get("alt_text") and stt_quality.assess(str(row["alt_text"]), secs):
            row = dict(row, alt_text="")
        sig = hash((text, str(row.get("alt_text") or "")))
        if self._seen.get(tid) == sig:
            if scoped:
                self._count(now, "repeat", scoped)
            return []
        if start and now - start > max_age:
            # older than max_age_min (a catch-up, a startup backfill): still read, as context and a record at log
            # ("late"), never a push, a re-push or a re-open (as a row #747 logged late)
            self.stale += 1
            if scoped:
                self._count(now, "too_old", scoped)
            row = dict(row, aged=True)        # (never "late": that is radio_traffic.late, an outage's, late_cause)
        if scoped and policy_mod.quiet(self.policy.cfg, now, self.tz):
            self._count(now, "quiet", scoped)
        self._again = tid in self._seen       # read before, on time or late (a re-text after the window let it go)
        self._seen[tid] = sig
        if len(self._seen) > SEEN_MAX:
            for k in list(self._seen)[: SEEN_MAX // 2]:
                del self._seen[k]
        out: list[Event] = []
        with self._lock:
            self._late_now = None             # (cause, would have pushed): this row read late, once overall
            for pack in self.packs:
                if pack.in_scope(row):
                    out += self._feed_pack(pack, row, tid, text)
            if self._late_now is not None:
                cause, lost = self._late_now
                for k in ("late", f"late_{cause}", *(["late_lost"] if lost else [])):
                    self._count(now, k, [], overall=True)
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
        tk = self.talkers.of(row)             # its radio as Settings > Radios names it (None: as before)
        console_cue = None                    # the console cue in shadow: recorded (a hit of weight 0), never scored
        if (tk is not None and tk.console and pack.console_cue != "off"
                and any(r.category in pack.categories and w > 0 for r, w in hits)):
            # a labelled dispatch console's call-out is a dispatch cue without the words: the pack's own cue rule, at
            # its own weight, once (a row that said the words has it already); "console_cue": on adds it, shadow
            # (the default) only records where it would have
            cue = self._dispatch_cue(pack)
            if cue is not None and cue.enabled and cue.mode != "off" and all(r.id != cue.id for r, _w in hits):
                if pack.console_cue == "on":
                    hits.append((cue, cue.w))
                else:
                    console_cue = cue
        who = tuple(x for x in (str(row.get("talker") or ""), str(row.get("radio_id") or ""),
                                *str(row.get("rig") or "").split(","), tk.label if tk else "") if x.strip())
        win = self.windows.setdefault((pack.name, key), Window())
        known = tid in win.entries
        talker = str(row.get("talker") or "").strip()
        units = {f"{m.group(1).lower()} {m.group(2).lower()}" for m in UNIT_MENTION.finditer(text)}
        units |= {u.strip().lower() for u in str(row.get("rig") or "").split(",") if u.strip()}
        disp = (bool(row.get("dispatcher")) or bool(tk and tk.console)
                or bool(talker and (DISPATCH_WORDS.search(talker) or talker in self._hq(row))))
        if talker and not disp and UNIT_MENTION.fullmatch(talker):
            units.add(talker.lower())
        end = max(at, float(row.get("end") or at))
        entry = Entry(at, hits, text, who, suppressed, talker, frozenset(units), disp, end=end,
                      ties=_talker_ties(row, talker, self.talkers.is_console),
                      label=tk.label if tk else "", town=self._talker_town(pack, key, tk),
                      late=bool(row.get("late") or row.get("aged")) or self.clock() - end > pack.late_s,
                      read=self.clock())
        # a re-text (#901's correction, a re-ask, a catch-up): the categories its old words said
        was = ({r.category for r, w in win.entries[tid].hits if w > 0 and not r.id.startswith(("model.", "infer."))}
               if known else set())
        win.entries[tid] = entry              # in the window before its place is read (_town_prior: the job's console)
        if (self.locate is not None or (pack.hw and self.highway is not None)) and not suppressed and (
                self._could_reach(pack, win.around(at, pack.window_s), tid, hits)
                or any(not e.confirmed or pack.category(e.category).get("moves")
                       or (e.category in PEOPLE and MOVE_WORDS.search(text))
                       for e in self._open_for(pack, None, key, at, self._merge_span(pack)))):
            entry.place = self._where(pack, row, key, [text], list(who))
        whole = win
        whole.prune(max(e.at for e in whole.entries.values()), self._keep_s(pack), keep=tid, now=self.clock())
        win = whole.around(at, pack.window_s)     # AIR-TIME order: the rows that aired around this one, whenever read
        changed: list[Event] = []
        self._lost_now = False                # _raise: this late row, on time, would have raised a new pre-alert
        if suppressed:
            changed += self._end_window(pack, key, at, tid, win, ws)
            win = whole.around(at, pack.window_s)     # (what the window held is spent: _consume)
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
        if not suppressed and pack.resolved and pack.resolves(text, ws):
            changed += [x for x in self._resolve(pack, key, at, tid) if not any(x is c for c in changed)]
        if not suppressed and pack.calm:
            for x in (self._uncalm(pack, key, at, tid, ws, win.entries[tid], event_of)
                      + self._deescalate(pack, row, key, at, tid, text, ws, win.entries[tid])):
                if not any(x is c for c in changed):
                    changed.append(x)
        if entry.late and not self._again and can_push(pack) and self.counters.first_late(tid, pack.name, self.clock()):
            cause, lost = late_cause(row), self._lost_now     # a push source's text read late: by cause (late_row),
            self._late_now = (cause, lost or bool(self._late_now and self._late_now[1]))      # overall only (feed)
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
        if (self.infer is not None and hits and not suppressed and pack.src.get("infer", True)   # ("infer": false: rules only)
                and not win.entries[tid].late                                        # (a late row: context only)
                and not (self.backing is not None and self.backing.owns(pack))):     # one model per row (backing.py)
            try:
                changed += self._inference(pack, row, key, tid, win, at, cats, mine, event_of, hits)
            except Exception as e:  # noqa: BLE001 - the model never stops the rules
                self.log(f"intel: inference: {type(e).__name__}")
        keep = {r.id for r, _w in hits} | ({f"{console_cue.id}.console"} if console_cue is not None else set())
        if known and hasattr(self.sink, "clear"):
            self.sink.clear(tid, keep)
        if console_cue is not None:
            self.sink.hit(Hit(tid, f"{console_cue.id}.console", console_cue.v, 0.0, rules_mod.WILD, pack.name, at,
                              None))
        for r, w in hits:
            ev = event_of.get(r.category)
            if ev is None and r.category in (rules_mod.WILD, "routine") and len(event_of) == 1:
                ev = next(iter(event_of.values()))
            self.sink.hit(Hit(tid, r.id, r.v, round(float(w), 4), r.category, pack.name, at, ev.id if ev else None))
        # a re-text that no longer says a category: that category's events lose this row (scored again without it)
        for ev in [e for e in self.events.open_events() if e.pack == pack.name and tid in e.traffic_ids
                   and e.category in was - {r.category for r, w in hits if w > 0} - {rules_mod.WILD, "routine"}]:
            x = self._unsupport(pack, ev, tid, drop=False)
            if not any(x is c for c in changed):
                changed.append(x)
        if self.backing is not None:          # last: only a row the rules raised nothing with is asked (backing.py)
            try:
                changed += self.backing.consider(self, pack, row, key, tid, win, at, suppressed, hits, changed,
                                                 ruled=over)
            except Exception as e:  # noqa: BLE001 - the model never stops the rules
                self.log(f"intel: model: {type(e).__name__}")
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
                and e.last_at >= at - span and e.first_at <= at + span and (not e.until or e.until >= at)]

    def _merge_span(self, pack) -> float:
        """How far back an open event of the same pack, category and source takes a new mention (#492 review, H3)."""
        return max(pack.window_s, float(self.policy.cfg.get("merge_min", 20)) * 60)

    def _keep_s(self, pack) -> float:
        """How long a source's rows are kept for context (by air time, from its newest): any row still young enough
        to be read (max_age_min) finds the rows that aired within window_s of it."""
        return pack.window_s + float(self.policy.cfg.get("max_age_min", 15)) * 60

    def _aired(self, pack, ev: Event) -> float:
        """The air time of the event's newest row at hand (a disregard or a de-escalation in its traffic too), else
        its last_at: a row that aired before it never undoes what that row said (air-time order)."""
        win = self.windows.get((pack.name, ev.source_key))
        return max([ev.last_at, *(win.entries[t].at for t in ev.traffic_ids if win is not None and t in win.entries)])

    def _fresh(self, pack, ev: Event) -> bool:
        """The event may be pushed a first time: its rows read in time (never late, Entry.late) reach the threshold of
        its category on their own, the newest of them ended within the pack's late_s. A late row corroborates (a place, a
        read-back, the score shown), it never makes a fresh row's event push."""
        if self._as_fresh:
            return True                       # (_on_time_push: as if read on time)
        rows = {t: e for (p, _k), w in self.windows.items() if p == pack.name
                for t, e in w.entries.items() if t in ev.traffic_ids and not e.late}
        if not rows or self.clock() - max(e.end for e in rows.values()) > pack.late_s:
            return False                      # (its newest row read in time ended over late_s ago)
        got = self._scores(pack, Window(rows)).get(ev.category)
        cd = pack.category(ev.category)
        thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
        return got is not None and got[0] >= thr

    def _on_time_push(self, pack, ev: Event, mode: str, now: float) -> bool:
        """This new event, raised by a late row, would have been pushed had the row been read on time: its holds
        (out of area, our call, the FD, a place) judged as if fresh, pushes on, no quiet hours, no cap. What "would have
        pushed" counts (late_row): a pre-alert lost, never a follow-up of one already pushed."""
        probe = copy.deepcopy(ev)
        probe.suppressed, probe.mode = "", mode
        self._as_fresh = True
        try:
            self._hold(pack, probe, mode)
        finally:
            self._as_fresh = False
        return (probe.mode == "push" and not probe.suppressed and bool(self.policy.cfg.get("push", True))
                and not policy_mod.quiet(self.policy.cfg, now, self.tz) and not self.policy._over(ev.category, now))

    def _read_before(self, tid: str) -> bool:
        """This row was read (since the start; or before a restart: it is in an open event)."""
        return tid in self._seen or any(tid in e.traffic_ids for e in self.events.open_events())

    def _withdraw(self, row: dict, tid: str) -> list[Event]:
        """A row read before whose text is empty or "unclear" now (#890's re-ask of a loop or noise): its hits go
        (sink.clear), it leaves the window and the model's context and queue, and each open event it is in loses it
        (_unsupport). Its next text is read afresh."""
        self._seen.pop(tid, None)
        out: list[Event] = []
        with self._lock:
            for pack in self.packs:
                if not pack.in_scope(row):
                    continue
                win = self.windows.get((pack.name, str(row.get("source") or row.get("source_key") or "")))
                if win is not None:
                    win.entries.pop(tid, None)
                if self.backing is not None and hasattr(self.backing, "forget"):
                    self.backing.forget(tid)
                out += [self._unsupport(pack, ev, tid) for ev in self.events.open_events()
                        if ev.pack == pack.name and tid in ev.traffic_ids]
            if hasattr(self.sink, "clear"):
                self.sink.clear(tid, set())
        return out

    def _unsupport(self, pack, ev: Event, tid: str, drop: bool = True) -> Event:
        """The event without this row's words: scored again over its other rows (WHAT, level, rules; never its
        first_at, mode or pushes). When none of them reaches the threshold alone it rested on this row: noted
        WITHDRAWN, and ended if never pushed (out of the digest too); a pushed one stays open with the note, never
        un-pushed. Rows of it no longer at hand (past the window's keep) are taken as still backing it. drop: the row
        leaves its traffic (withdrawn); a re-text stays in it, its new words no longer this category's."""
        now = self.clock()
        rest = [t for t in ev.traffic_ids if t != tid]
        win = self.windows.get((pack.name, ev.source_key))
        have = {t: win.entries[t] for t in rest if win is not None and t in win.entries}
        got = self._scores(pack, Window(have)).get(ev.category) if len(have) == len(rest) else None
        cd = pack.category(ev.category)
        thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
        if drop:
            ev.traffic_ids = rest
        if got is not None and got[0] >= thr:
            ev.what_score, ev.level = got[0], level(got[0])
            ev.rules = [list(x) for x in sorted({(r.id, r.v, round(float(w), 4)) for _t, r, w in got[1]})]
        elif len(have) == len(rest):
            ev.note = WITHDRAWN
            if ev.pushed_at is None:
                ev.status = "ended"
                if ev.id in self.policy.held:
                    self.policy.held.remove(ev.id)
                    self.save_policy()
        ev.updated_at = now
        self.events.save(ev)
        self.sink.event(ev, "ended" if ev.status == "ended" else "updated")
        return ev

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

    def _event_entries(self, pack, ev: Event, at: float | None = None) -> list:
        """The event's rows at hand; with at, only those that aired within window_s of it (a tie by unit or talker is
        by AIR time: never to a row of the same talker a quarter of an hour away)."""
        win = self.windows.get((pack.name, ev.source_key))
        return [win.entries[t] for t in ev.traffic_ids if win is not None and t in win.entries
                and (at is None or abs(win.entries[t].at - at) <= pack.window_s)]

    def _same_job(self, pack, ev: Event, entry, ws: list[str], mine=frozenset()) -> bool:
        """A transmission is about this event only when (#492 re-check):
             it names a unit or car already tied to the event ("car 3 to dispatch, I'm at ..."),
             or repeats the event's category words,
             or reads back the event's place,
             or the same talker said it (the same radio ID: _talker_ties), when that talker is known and is no
               dispatcher or HQ (who voices every job).
        A row with another category's rule words is another job."""
        if mine - {ev.category, rules_mod.WILD, "routine"}:
            return False
        if self.category_words(pack, ev.category) & set(ws):
            return True
        if entry.place is not None and ev.precision != "none" and same_place(entry.place, ev.place()):
            return True
        mine_entries = self._event_entries(pack, ev, entry.at)
        shared = frozenset(u for u in entry.units & frozenset(u for e in mine_entries for u in e.units)
                           if not _unit_placeholder(u))
        if shared and not self._busy(pack, ev, shared, entry.at):
            return True                       # (a unit on two or more open jobs: only words or the place tie it)
        if not entry.dispatcher and _ties_with(entry.ties, [e.ties for e in mine_entries if not e.dispatcher]):
            return True
        # "now at", "last seen at": the one moving incident open on this source
        movable = lambda e: pack.category(e.category).get("moves") or e.category in PEOPLE      # noqa: E731
        if MOVE_WORDS.search(entry.text) and movable(ev):
            return [e.id for e in self._open_for(pack, None, ev.source_key, entry.at, self._merge_span(pack))
                    if movable(e)] == [ev.id]
        return False

    def _busy(self, pack, ev: Event, units, at: float | None = None) -> bool:
        """These units are on another open job too, around the row's air time (then they tie by words or place only)."""
        for e in self._open_for(pack, None, ev.source_key, self.clock() if at is None else at, self._merge_span(pack)):
            if e.id != ev.id and units & frozenset(u for x in self._event_entries(pack, e, at) for u in x.units):
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
        prior = self._town_prior(pack, row, key)
        return {"source_key": key, "system": str(row.get("system") or ""), "service": str(row.get("service") or ""),
                "towns": own or list(pack.area), "only": bool(pack.area) and not own, "pack": pack.name,
                **({"prefer": [prior]} if prior else {})}

    @staticmethod
    def _dispatch_cue(pack):
        """The pack's dispatch cue rule ("<prefix>dispatch_cue", a "*" cue), or None."""
        return next((r for r in pack.rules
                     if r.category == rules_mod.WILD and r.id.rsplit(".", 1)[-1] == "dispatch_cue"), None)

    def _talker_town(self, pack, key: str, tk) -> str:
        if tk is None:
            return ""
        return talkers_mod.town_of(tk, pack.towns_for(key) or list(pack.area), pack.agency_towns)

    def _town_prior(self, pack, row: dict, key: str) -> str:
        """The town a source shared by several towns most likely means (place.py's "prefer"), "" for none (as before):
          1. this row's own department's (its talker's label: a console's or an officer's);
          2. else the window's labelled consoles', only when every one of them says the same town and one of them is
             tied to this row's job (the same street, a unit or a category both name);
          3. else none."""
        if len(pack.towns_for(key) or pack.area) < 2:
            return ""
        own = self._talker_town(pack, key, self.talkers.of(row))
        if own:
            return own
        win = self.windows.get((pack.name, key))
        if win is None:
            return ""
        win = win.around(float(row.get("start") or self.clock()), pack.window_s)   # the rows that aired around it
        tid = str(row.get("id") or "")
        consoles = [e for t, e in win.entries.items() if t != tid and e.dispatcher and e.label and not e.suppressed]
        towns = {e.town for e in consoles}
        if len(towns) != 1 or "" in towns:
            return ""
        me = win.entries.get(tid)
        text = me.text if me is not None else str(row.get("text") or "")
        units = me.units if me is not None else frozenset(
            f"{m.group(1).lower()} {m.group(2).lower()}" for m in UNIT_MENTION.finditer(text))
        cats = {r.category for r, w in (me.hits if me is not None else ()) if w > 0 and r.category in pack.categories}
        streets = street_names(text)
        for c in consoles:
            if ((streets & street_names(c.text)) or (units & c.units)
                    or (cats & {r.category for r, w in c.hits if w > 0 and r.category in pack.categories})):
                return next(iter(towns))
        return ""

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

    def _fire(self, pack, ev: Event) -> str:
        """A fire-related event with the FD already there (the module's doc, step 6): links it (near_incident), sets
        ev.fd and, for an FYI, its priority. Returns why it is held ("fd_on_scene" | "fd_dispatched" | "fd_there"),
        or "" (no FD there, the FD cleared, or an FYI: pushed as one). An event pushed as a pre-alert is linked only:
        never held, never made an FYI; one pushed as an FYI stays one, at its FYI priority."""
        texts = self._texts(pack, ev.source_key, ev.traffic_ids)
        inc = self._fire_incident(pack, ev, texts)
        if inc is not None:
            ev.near_incident = int(inc["id"])
        prio = pack.fd or {}
        safety = pack.fd_said("safety", texts)
        if ev.pushed_at is not None:
            if ev.fd in FD_FYI:               # pushed as an FYI: never re-titled, never back to the category's priority
                ev.priority = int(prio.get("safety_priority", 4) if safety else prio.get("fyi_priority", 2))
            return ""
        state = str((inc or {}).get("fd") or "")
        if (inc is not None and not state and inc.get("closed_at") is None
                and ev.first_at - float(inc.get("created_at") or 0.0) <= FD_PAGED_S):
            state = "dispatched"              # open, no unit known yet, made just now: the FD is already toned out
        if state not in ("on_scene", "dispatched") and pack.fd_said("there", texts):
            state = "there"                   # said so: another department's call, or ours not in the data yet
        ev.fd = state if state in (*FD_FYI, "cleared") else ""
        if ev.fd in ("", "cleared"):
            return ""                         # no FD there now (a rekindle after it cleared): a pre-alert as before
        if safety:
            ev.priority = int(prio.get("safety_priority", 4))
            return ""
        if ev.fd == "on_scene":
            return "fd_on_scene"
        if pack.fd_said("add_info", texts):
            ev.priority = int(prio.get("fyi_priority", 2))
            return ""
        return f"fd_{ev.fd}"

    def _fire_incident(self, pack, ev: Event, texts) -> dict | None:
        """Our fire incident at a fire-related event's place (_fire): of a type the pack's "fd_present" "types" allow
        for the category; open and opened at most FD_OPEN_S before the event, or closed at most fd_window_min before it
        (and made at most INCIDENT_AFTER_S after its last transmission); at the same address or block, or (a confirmed
        address or intersection) within fd_radius_m and half the place's radius. With no place: an open one whose
        street the words name in its town (fire_street_said). Ranked: the FD on scene, dispatched, cleared; then open;
        then the newest."""
        if self.fire_incidents is None:
            return None
        win = float(self.policy.cfg.get("fd_window_min", 60)) * 60
        rad = float(self.policy.cfg.get("fd_radius_m", 150))
        types = (pack.fd or {}).get("types") or {}
        allowed = types.get(ev.category, types.get("*"))
        try:
            incs = self.fire_incidents(ev.first_at - FD_OPEN_S, ev.first_at - win) or []
        except Exception as e:  # noqa: BLE001 - no incidents: pushed as before
            self.log(f"intel: fire incidents: {type(e).__name__}")
            return None
        placed = ev.precision != "none" and ev.lat is not None and ev.lon is not None
        said = towns_said(texts) if not placed else set()
        own = {t.lower() for t in pack.towns_for(ev.source_key)}
        best = None
        for inc in incs:
            at = float(inc.get("created_at") or 0.0)
            closed = inc.get("closed_at")
            if not at or at > ev.last_at + INCIDENT_AFTER_S:
                continue
            if closed is None and at < ev.first_at - FD_OPEN_S:
                continue
            if closed is not None and float(closed) < ev.first_at - win:
                continue
            if allowed is not None and str(inc.get("type") or "").upper() not in allowed:
                continue                      # not a fire incident (an aided, an MVA, a drill): never the FD at a fire
            if placed:
                hit = fire_place(ev, inc, rad)
            else:
                hit = closed is None and fire_street_said(inc, texts, said, own)
            if not hit:
                continue
            if len(self._numbers) > 500:
                self._numbers.clear()         # (a few a day: never grows without bound)
            self._numbers[int(inc["id"])] = (str(inc.get("number") or ""), inc.get("fd_at"))
            rank = (FD_RANK.get(str(inc.get("fd") or ""), 0), closed is None, at)
            if best is None or rank > best[0]:
                best = (rank, inc)
        return best[1] if best else None

    def _cleared_since(self, ev: Event) -> bool:
        """The FD cleared the event's incident after its first transmission (a rekindle while the call was held):
        let go as a pre-alert, never fd_late."""
        if ev.fd != "cleared" or not ev.near_incident:
            return False
        at = self._numbers.get(int(ev.near_incident), ("", None))[1]
        return at is None or float(at) > ev.first_at

    def fd_shown(self, ev_fd: str, near) -> str:
        """Event.fd as the console says it, from what the lookup last gave ("FD on scene · #26-000041")."""
        number, at = self._numbers.get(int(near), ("", None)) if near else ("", None)
        return push_mod.fd_text(ev_fd, number, at, self.tz)

    def _hold(self, pack, ev: Event, mode: str | None = None) -> None:
        """Out of area, or our own incident's: held at log (the module's doc, step 6). Only a pack with an area (the
        county-wide packs): the police pack's events are linked only in a "fire_related" category (_fire), and held
        only there or in a "place": true category (unplaced, _unplaced). An event already pushed is never held
        (its mode never goes down: it stays on the map and the console). mode: what the rules say now, given back when
        a hold ends (a moving event leaving our incident's place)."""
        if ev.pushed_at is None and not self._fresh(pack, ev):
            ev.suppressed, ev.mode = "late", lowest(ev.mode, "log")   # late rows are context, never a push's origin:
            return                                                    # recorded at log
        cat = pack.category(ev.category)
        if cat.get("highway"):
            self._hw_hold(pack, ev, mode)
            return
        if not pack.area and not cat.get("place") and not cat.get("fire_related"):
            return
        if ev.suppressed in ("placed_late", "fd_late") and ev.pushed_at is None:
            ev.mode = lowest(ev.mode, "log")  # placed (or let go by the FD) too late to be a pre-alert: stays at log
            return
        if pack.area:
            ev.near_incident = self._incident(ev)
        why = "out_of_area" if self._outside(pack, ev) else self._unplaced(pack, ev)
        fd = self._fire(pack, ev) if cat.get("fire_related") else None     # linked whatever else holds it
        if fd is not None:
            why = why or fd
        elif not why and ev.near_incident is not None and not cat.get("safety"):
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
        elif (ev.suppressed in FD_HELD and ev.fd not in FD_FYI and not self._cleared_since(ev)
              and ev.last_at - ev.first_at > RELEASE_S):
            ev.suppressed = "fd_late"         # held for the FD, and the incident gone since (dismissed, no longer
            ev.mode = lowest(ev.mode, "log")  # at the place): let go too late to be a pre-alert, as placed_late
        elif ev.suppressed in ("out_of_area", "near_incident", *FD_HELD):
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
        return _ties_with(entry.ties, talkers) or bool(entry.units & units)

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
        """The talkers (each row's ties: _talker_ties, _ties_with) and units of an unplaced event's own transmissions
        (kept past the window, _note_who)."""
        talkers, units = self._who_of.get(ev.id, (set(), frozenset()))
        mine = [e for e in self._event_entries(pack, ev)]
        return (talkers | {e.ties for e in mine if e.ties},
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

    def _rules_mode(self, pack, ev: Event, at: float | None = None) -> str | None:
        """The mode the window's rules give the event's category now (pack, category and rung), None: not reached.
        The rows that aired within window_s of `at` (the row being read; else the event's last)."""
        win = self.windows.get((pack.name, ev.source_key))
        win = win.around(ev.last_at if at is None else at, pack.window_s) if win is not None else None
        got = self._scores(pack, win).get(ev.category) if win is not None else None
        if got is None:
            return None
        cd = pack.category(ev.category)
        thr = cd.get("threshold") if cd.get("threshold") is not None else pack.threshold
        return lowest(pack.mode, cd.get("mode", "push"), got[2]) if got[0] >= thr else None

    def _where(self, pack, row: dict, key: str, texts: list[str], who=()):
        """The place in these transmissions' words (only the event's own: never another job's in the same window);
        a label the scrub refuses (a plate or an ID read as an address) is no place at all (fail closed)."""
        if pack.hw and texts:
            return self._hw_where(texts)      # a highway pack: its own places only (highway.py), never a street's
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
        after = ev is not None and at >= self._aired(pack, ev)    # aired after the disregard / de-escalation (FIFO)
        if ev is not None and ev.note == "possibly cancelled" and speaks and after and not entry.late:
            reopened = True                   # a nameless disregard, then the call again: it stands, pushed again
            ev.note = "re-opened"
        elif ev is not None and ev.note == WITHDRAWN and speaks:
            ev.note = ""                      # its words are back (a re-ask's clean text, another row saying it)
        elif ev is not None and ev.note == DEESCALATED and speaks and after:
            ev.note = ""                      # the weapon is back in it: the note goes; a safety category's escalation
            if cd.get("safety") and pack.escalates(cat, entry.text, rules_mod.words(entry.text)) and not entry.late:   # ("pulled out a gun",
                reopened, ev.note = True, "re-opened"                                               # never "in custody")
        if ev is None and not speaks:
            return None                       # a cue alone (another job's "respond to ...") never makes an event
        if ev is None and hasattr(self.events, "bound") and self.events.bound(tid, pack.name, cat):
            return None                       # already an event's (one expired, a re-feed after a restart): never two
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
        if ev is None and not fresh and place is not None and cd.get("highway"):
            ev = self._hw_same(pack, cat, place, at)   # another source's report of this crash: one event, one push
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
                       dept=str(row.get("dept") or ""), status="open",
                       # its first transmission by AIR time (the rows that say it, whatever order they were read in)
                       first_at=min([at, *(win.entries[t].at for t in tids if t in win.entries)]))
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
        if entry.late:
            ev.until = max(ev.until, now + pack.window_s)    # its air-time neighbours, read just after, join it
        if place is not None and (moved or ev.precision == "none" or place.confirmed or place.where_score > ev.where_score):
            ev.set_place(place)
            if moved:
                ev.note = "moved"
        ev.subject = f"{pack.name}:{cat}:{ev.place().key or key}"
        self._hold(pack, ev, mode)
        if entry.late and change == "raised":
            self._lost_now = self._lost_now or self._on_time_push(pack, ev, mode, now)
            ev.suppressed, ev.mode = "late", lowest(ev.mode, "log")   # logged late (#747): a record, never a push
        if before is not None and _same(before, ev):
            return None                       # idempotent: the same row again changes nothing
        ev.updated_at = now
        self.events.save(ev)
        self._note_who(pack, ev)
        self._after(pack, ev, change, now, before, moved=moved and before is not None and before.precision != "none"
                    and not same_place(before.place(), ev.place()), tid=tid, at=at)
        if cd.get("highway"):
            self._hw_promote(pack, ev, key, now)
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
        self._hold(pack, ev, self._rules_mode(pack, ev, at))
        if _same(before, ev):
            return None
        ev.updated_at = self.clock()
        self.events.save(ev)
        self._note_who(pack, ev)
        self._after(pack, ev, "updated", ev.updated_at, before, moved=move, tid=tid, at=at)
        if pack.category(ev.category).get("highway"):
            self._hw_promote(pack, ev, key, ev.updated_at)
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
            win = win.around(at, pack.window_s) if win is not None else None     # the rows that aired around it
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
        # by AIR time: only the rows that aired before it, and never an event that began after it (a call read
        # before a disregard that aired earlier)
        current = {t for t, e in win.entries.items() if t != tid and e.at <= at} if win is not None else set()
        entry = win.entries.get(tid) if win is not None else None
        open_ = [e for e in self._open_for(pack, None, key, at, self._merge_span(pack)) if e.first_at <= at]
        named = [ev for ev in open_ if entry is not None and self._same_job(pack, ev, entry, list(ws))]
        # what it names (a unit, a place, the category's words); naming nothing, the current window's own events, but
        # never a safety call (shots, a barricade, a fire, a water rescue): that one is only "possibly cancelled"
        # a call whose category has "cancel_ttl_min" (EDP, a dispute) flagged by an earlier disregard: this one ends it
        again = [] if named else [ev for ev in open_ if ev.note == "possibly cancelled" and self._soft(pack, ev.category)]
        for ev in [] if named else [ev for ev in open_ if current & set(ev.traffic_ids) and self._safe(pack, ev.category)
                                    and ev not in again]:
            now = self.clock()
            ev.note, ev.updated_at = "possibly cancelled", now
            ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
            if self._soft(pack, ev.category):            # "cancel_ttl_min": it lives no longer than this after it
                ev.until = min(ev.until or now, now + float(pack.category(ev.category)["cancel_ttl_min"]) * 60)
            self.events.save(ev)
            self.sink.event(ev, "updated")
            out.append(ev)
        for ev in (named or [ev for ev in open_ if current & set(ev.traffic_ids) and not self._safe(pack, ev.category)]) + again:
            ev.status, ev.updated_at = "ended", self.clock()
            ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
            self.events.save(ev)
            self.sink.event(ev, "ended")
            out.append(ev)
        self._consume(pack.name, key, at)
        return out

    def _deescalate(self, pack, row: dict, key: str, at: float, tid: str, text: str, ws: list[str], entry) -> list[Event]:
        """A category's "deescalate" phrase (rules.py; the weapons category: "the gun was secured", "the firearm
        has been recovered"): its open event on this source within merge_min (the span an open
        event takes a new mention: the follow-up often comes once units are there) is noted DEESCALATED, with this row
        in its traffic. Only an event this row ties to (_same_job: the category's own words, a unit, the talker, the
        place), and with more than one such event open, only the one at the place this row says (none said or none
        there: nothing is marked); the same when the row speaks of another job ("at the other call") or names only
        units the event's rows don't. Never a row that itself says the category ("the gun was secured, the second
        male has a machete"), never over a "possibly cancelled" note, never a push, an end or a new event. A later row that says the
        category again clears the note (_raise), and pushes again when it alone is over the threshold."""
        out = []
        for cat in pack.deescalated(text, ws):
            if any(r.category == cat and w > 0 for r, w in entry.hits):
                continue
            open_ = [e for e in self._open_for(pack, cat, key, at, self._merge_span(pack))
                     if self._same_job(pack, e, entry, ws, {r.category for r, w in entry.hits if w > 0})]
            if len(open_) > 1 or any(self._elsewhere(pack, e, entry) for e in open_):
                if entry.place is None and self.locate is not None:
                    entry.place = self._where(pack, row, key, [text], list(entry.who))
                open_ = [e for e in open_ if entry.place is not None and e.precision != "none"
                         and same_place(entry.place, e.place())]
                if len(open_) != 1:
                    continue
            if not open_ or open_[0].note in (DEESCALATED, "possibly cancelled") or at < self._aired(pack, open_[0]):
                continue                      # (aired before the event's newest row: read late, it undoes nothing)
            ev = open_[0]
            ev.note, ev.updated_at = DEESCALATED, self.clock()
            ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
            self.events.save(ev)
            self.sink.event(ev, "updated")
            out.append(ev)
        return out

    def _uncalm(self, pack, key: str, at: float, tid: str, ws: list[str], entry, event_of: dict) -> list[Event]:
        """A row that says a category again, not raised (under the threshold by itself: the call-out's rows have left
        the window): the note DEESCALATED on its open event comes off; a safety category's escalation ("escalate",
        rules.py) re-opens it and pushes it again, as _raise does within the window."""
        out = []
        mine = {r.category for r, w in entry.hits if w > 0}
        for cat in mine & set(pack.calm) - set(event_of):
            for ev in self._open_for(pack, cat, key, at, self._merge_span(pack)):
                if ev.note != DEESCALATED or at < self._aired(pack, ev) or not self._same_job(pack, ev, entry, ws, mine):
                    continue                  # (a row that aired before the de-escalation, read late: undoes nothing)
                now = self.clock()
                if pack.is_safety(cat) and pack.escalates(cat, entry.text, ws) and not entry.late:
                    before = copy.deepcopy(ev)
                    ev.note, ev.last_at, ev.updated_at = "re-opened", max(ev.last_at, at), now
                    ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
                    self.events.save(ev)
                    self._after(pack, ev, "reopened", now, before, tid=tid, at=at)
                else:
                    ev.note, ev.updated_at = "", now
                    self.events.save(ev)
                    self.sink.event(ev, "updated")
                out.append(ev)
        return out

    def _safe(self, pack, cat: str) -> bool:
        """A safety call: never ended by a disregard that names nothing, only "possibly cancelled". The engine's own
        list, and in a police pack every category the pack marks "safety" (EDP, a dispute, an officer); the fire and
        EMS packs keep the engine's list only."""
        return cat in SAFETY or (pack.domain == "police" and pack.is_safety(cat))

    def _soft(self, pack, cat: str) -> bool:
        """A safety call a second disregard or a "resolved" phrase ends ("cancel_ttl_min": EDP, a dispute)."""
        return self._safe(pack, cat) and pack.category(cat).get("cancel_ttl_min") is not None

    def _resolve(self, pack, key: str, at: float, tid: str) -> list[Event]:
        """A "resolved" phrase ("unfounded", "the caller called back"): the source's "possibly cancelled" calls with
        "cancel_ttl_min" end."""
        out = []
        for ev in self._open_for(pack, None, key, at, self._merge_span(pack)):
            if ev.note == "possibly cancelled" and self._soft(pack, ev.category):
                ev.status, ev.updated_at = "ended", self.clock()
                ev.traffic_ids = list(dict.fromkeys([*ev.traffic_ids, tid]))
                self.events.save(ev)
                self.sink.event(ev, "ended")
                out.append(ev)
        return out

    def _elsewhere(self, pack, ev: Event, entry) -> bool:
        """The row speaks of another job ("at the other call"), or names units and none of the event's own."""
        if OTHER_JOB.search(entry.text or ""):
            return True
        mine = frozenset(u for e in self._event_entries(pack, ev, entry.at) for u in e.units)
        return bool(entry.units) and not entry.units & mine

    def _consume(self, pack_name: str, key: str, at: float | None = None) -> None:
        """What the window held is spent: a routine cue after "disregard" never adds it up again (#492 review, H2).
        at: a disregard's air time: only the rows that aired up to it (one aired after it is the next call's)."""
        win = self.windows.get((pack_name, key))
        if win is not None:
            win.entries = {t: e for t, e in win.entries.items() if e.suppressed or (at is not None and e.at > at)}

    def dismissed(self, ev: Event) -> None:
        """A person dismissed it (sizeup/intel/admin.py): its window is spent, and mute_min keeps it from coming back."""
        with self._lock:
            self._consume(ev.pack, ev.source_key)

    def _after(self, pack, ev: Event, change: str, now: float, before: Event | None, moved: bool = False,
               tid: str = "", at: float | None = None) -> None:
        self.sink.event(ev, "updated" if change == "reopened" else change)
        if change == "raised":
            self._count(now, "events", [pack.name])
        was = ev.suppressed
        d = self.policy.decide(ev, change, now, before, moved)
        win = self.windows.get((pack.name, ev.source_key))
        if d.would_push and tid and win is not None and tid in win.entries and win.entries[tid].late:
            d = Decision(d.mode, False, "late", d.priority)   # a late row (read late, #747's) never pushes
        elif (d.would_push and ev.pushed_at is not None and before is not None and at is not None
                and tid in before.traffic_ids):
            d = Decision(d.mode, False, "late", d.priority)   # a re-text of its own row, or a row read late: never a
                                                              # push again, out of air-time order
        if d.would_push:
            cat = pack.category(ev.category)
            doubtful = push_mod.doubtful_number(ev.label_full, self._texts(pack, ev.source_key, ev.traffic_ids))
            # push_pii (Settings, on by default; admin decision 2026-11-17): no PII scrub, access is limited instead
            pii = self.policy.cfg.get("push_pii", True)
            msg = push_mod.compose(ev, pack.label, cat, self.public_url, self.tz, doubtful=doubtful,
                                   number=self._numbers.get(ev.near_incident or 0, ("", None))[0],
                                   fd_at=self._numbers.get(ev.near_incident or 0, ("", None))[1],
                                   heard=self._heard(pack, ev) if pii else "")
            why = "" if pii else push_mod.scrub(msg, self._who(pack, ev))
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

    def _heard(self, pack, ev: Event) -> str:
        """Who the event was heard from, as Settings > Radios labels the radio (talkers.py): its dispatch console's,
        else its first labelled talker's; "" when none is labelled (never a raw radio ID)."""
        es = self._event_entries(pack, ev)
        return (next((e.label for e in es if e.label and e.dispatcher), "")
                or next((e.label for e in es if e.label), ""))

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

    # -- a "highway": true category (sizeup/intel/highway.py) ----------------------------------------------------

    def _corridor(self):
        h = self.highway
        try:
            return h() if callable(h) else h
        except Exception as e:  # noqa: BLE001 - no corridor now: such events stay unplaced (held)
            self.log(f"intel: highway: {type(e).__name__}")
            return None

    def _hw_where(self, texts: list[str]):
        """The highway place the words say (an exit, a marker, an interchange, the route alone), else None."""
        cor = self._corridor()
        if cor is None:
            return None
        try:
            return cor.place(cor.parse(" . ".join(texts)))
        except Exception as e:  # noqa: BLE001 - no place is still an event (held)
            self.log(f"intel: highway place: {type(e).__name__}")
            return None

    def _hw_towns(self, pack, ev: Event) -> list[str]:
        from sizeup.intel.highway import towns_of
        return towns_of(ev.dept, pack.towns_for(ev.source_key))

    def _hw_hold(self, pack, ev: Event, mode: str | None = None) -> None:
        """Held at log until the crash is on a highway segment by our district (highway.Corridor.gate: "out_of_area",
        "unplaced"), or our own call is already at it ("near_incident": no pre-alert for the call we have). Pushed and
        our call there since: linked and kept at map (no more pushes, still on the map and the console)."""
        cor = self._corridor()
        hw = pack.hw
        why = cor.gate(ev.place(), self._hw_towns(pack, ev), hw["buffer_m"]) if cor is not None else "unplaced"
        inc = self._hw_incident(pack, ev, cor) if not why else None
        if inc is not None:
            ev.near_incident = inc
        if ev.pushed_at is not None:
            if ev.near_incident is not None:
                ev.mode, ev.note = lowest(ev.mode, "map"), "dispatched"
            return
        if why or ev.near_incident is not None:
            ev.mode = lowest(ev.mode, "log")
            ev.suppressed = why or "near_incident"
        elif ev.suppressed in ("out_of_area", "unplaced", "near_incident"):
            ev.suppressed = ""                # placed by our district now: the mode its rules raised it at
            ev.mode = max(ev.mode, mode if mode is not None else self._event_mode(pack, ev), key=mode_rank)

    def _hw_incs(self) -> list | None:
        if self.incidents is None:
            return None
        try:
            return list(self.incidents(self.clock()) or [])
        except Exception as e:  # noqa: BLE001 - no incidents is still a pre-alert
            self.log(f"intel: incidents: {type(e).__name__}")
            return None

    def _hw_incident(self, pack, ev: Event, cor=None, incs=None) -> int | None:
        """Our own incident at the crash (highway.Corridor.at_incident), made from an hour before its first report to
        link_min after its last; the latest. None: none (or no incidents known here). incs: as _hw_incs() read them."""
        cor = cor if cor is not None else self._corridor()
        if cor is None or ev.precision == "none":
            return None
        incs = incs if incs is not None else self._hw_incs()
        if incs is None:
            return None
        hw, place, best = pack.hw, ev.place(), None
        for inc in incs:
            at = float(inc.get("created_at") or 0.0)
            closed = inc.get("closed_at")
            if not at or at < ev.first_at - 3600.0 or at > ev.last_at + hw["link_min"] * 60:
                continue
            if closed is not None and float(closed) < ev.first_at:
                continue
            try:
                hit = cor.at_incident(place, inc, hw["link_m"], hw["buffer_m"])
            except Exception:  # noqa: BLE001
                hit = False
            if hit and (best is None or at > best[0]):
                best = (at, int(inc["id"]))
        return best[1] if best else None

    def _hw_same(self, pack, cat: str, place, at: float) -> Event | None:
        """The open crash event of this pack another source reported at the same place (highway.Corridor.same), within
        merge_min: the most recent."""
        cor = self._corridor()
        if cor is None:
            return None
        hw, best = pack.hw, None
        for e in self.events.open_events():
            if (e.pack != pack.name or e.category != cat or e.precision == "none" or e.last_at < at - hw["merge_min"] * 60
                    or e.suppressed in ("out_of_area", "unplaced")):
                continue                      # (another place's crash, or one not known to be ours: never this one)
            if cor.same(place, e.place(), hw["merge_m"]) and (best is None or e.last_at > best.last_at):
                best = e
        return best

    def _hw_promote(self, pack, ev: Event, key: str, now: float) -> None:
        """A crash pre-alert that went out (pushed: on a highway by our district): its source (and the reporting row's)
        transcribed like ours for promote_min (promoted_since(); the operating-channel promotion, sizeup/cad/opchannel.py),
        renewed by each later report but never past promote_max_min after the event's first promotion (it must not
        feed itself). An event's first promotion is raised from its first report (a new crash on a channel promoted
        before for another one too: its own backlog); a renewal of the same event after a lapse from LEAD_S before now
        only (#801's rule: promote_since covers the current promotion, never the backlog again). Never one held
        (out of area, unplaced), nor once our call is at it."""
        from sizeup.cad.opchannel import LEAD_S
        minutes = float(pack.hw.get("promote_min") or 0)
        if minutes <= 0 or ev.pushed_at is None or ev.near_incident is not None or ev.status != "open":
            return
        renewal = ev.id in self._promo_first          # this crash promoted before: from LEAD_S before now only
        first = self._promo_first.setdefault(ev.id, now)
        while len(self._promo_first) > 500:
            self._promo_first.pop(next(iter(self._promo_first)))
        until = min(now + minutes * 60, first + float(pack.hw.get("promote_max_min") or minutes) * 60)
        if until <= now:
            return
        added = False
        with self._promo_lock:
            for k in dict.fromkeys((ev.source_key, key)):
                if not k:
                    continue
                cur = self._promo.get(k)
                if cur is not None and cur[1] > now:
                    self._promo[k] = (cur[0], max(cur[1], until))      # live: extended, its since as it was
                    continue
                added = True                  # (a new crash on a channel promoted before: its own backlog)
                self._promo[k] = (now - LEAD_S if renewal else ev.first_at, until)
        if added and self.on_promote is not None:
            threading.Thread(target=self._promoted_cb, name="intel-promote", daemon=True).start()

    def _promoted_cb(self) -> None:
        try:
            self.on_promote()
        except Exception as e:  # noqa: BLE001
            self.log(f"intel: promote: {type(e).__name__}")

    def promoted_since(self, now: float | None = None) -> dict[str, float]:
        """{speech-to-text channel key ("tg:N", "p25:<system>:N"): since}: the sources a live crash pre-alert promotes
        now (_hw_promote), keyed as the station's queue keys them (sourcekeys.channel_key). Never under the engine's
        lock; {} on a station (its engine is idle)."""
        from sizeup import sourcekeys
        now = self.clock() if now is None else now
        out: dict[str, float] = {}
        with self._promo_lock:
            for k in [k for k, (_s, u) in self._promo.items() if u <= now]:
                del self._promo[k]
            items = list(self._promo.items())
        for k, (since, _u) in items:
            got = sourcekeys.parse(k)
            if got is not None:
                ck = sourcekeys.channel_key(*got)
                out[ck] = min(since, out.get(ck, since))
        return out

    def _hw_dispatched(self, now: float) -> list[Event]:
        """Pushed crash events our own call has come in for since (the county's dispatch, link_min): linked, kept at
        map, no more pushes (the tick: our call usually comes after the last report)."""
        out, incs = [], None
        for ev in self.events.open_events():
            pack = self.pack(ev.pack)
            if (pack is None or not pack.category(ev.category).get("highway") or ev.pushed_at is None
                    or ev.near_incident is not None):
                continue
            incs = self._hw_incs() if incs is None else incs        # one read a tick, only when there is such an event
            inc = self._hw_incident(pack, ev, incs=incs or [])
            if inc is None:
                continue
            ev.near_incident, ev.note, ev.mode, ev.updated_at = inc, "dispatched", lowest(ev.mode, "map"), now
            self.events.save(ev)
            self.sink.event(ev, "updated")
            out.append(ev)
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
                      if not w.entries or max(max(e.at, e.read) for e in w.entries.values()) < now - self._keep_for(k[0])]:
                del self.windows[k]
            if self.backing is not None and self.enabled:
                try:
                    out += self.backing.tick(self, now)       # held questions due, deadlines passed (backing.py)
                except Exception as e:  # noqa: BLE001 - the model never stops the tick
                    self.log(f"intel: model: {type(e).__name__}")
            if self.enabled:
                out += self._hw_dispatched(now)   # our own call at a pushed crash: linked, no more pushes
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

    def _keep_for(self, name: str) -> float:
        p = self.pack(name)
        return self._keep_s(p) if p is not None else 2 * self._span(name)


NOT_A_STREET = STOP_WORDS | {"my", "your", "his", "her", "their", "our", "its", "a", "an", "no", "any", "some", "on",
                             "in", "at", "to", "of", "same", "other", "next", "first", "last", "take", "took"}
STREET_NAME = re.compile(r"\b([a-z]+)\s+(?:" + "|".join(sorted(STREET_WORDS, key=len, reverse=True)) + r")\b")


def street_names(text: str) -> set[str]:
    """The streets a transmission names, by the word before the street word ("12 Sample Street" -> {"sample"})."""
    return {m.group(1) for m in STREET_NAME.finditer((text or "").lower()) if m.group(1) not in NOT_A_STREET}


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


# a talker text that names no one talker, the whole text: an unlabelled radio's ("unknown MRRS radio":
# sizeup/cad/radios.py), or a list of them; never an alias that only starts so ("UNKNOWN-4", "Unknown 12")
_GENERIC_ONE = r"(?:unknown(?: \w+)? radio|unknown|unk|unidentified)(?: caller)?"
GENERIC_TALKER = re.compile(rf"{_GENERIC_ONE}(?:\s*[,/&+]\s*{_GENERIC_ONE})*", re.I)


def _talker_ties(row: dict, talker: str, console=None) -> frozenset:
    """Who keyed up, as a tie by talker reads it: each of the row's radio IDs as "<system>:<id>" ("a,b": one per
    key-up), never an unknown one (p25.UNKNOWN_SRC: "-1", "0") nor a dispatch console's (console(system, id): the
    registry's label, talkers.py; it answers every job); a row with no known radio ID, its talker text (an alias heard
    on the channel), never a generic one ("unknown MRRS radio"). Never a radio's label: every unlabelled radio has the
    same text, and a department's or a range's label is many radios' (the admin labels by department and role, not
    per car)."""
    system = str(row.get("system") or "")
    ids = [x.strip() for x in str(row.get("radio_id") or "").split(",") if x.strip() not in UNKNOWN_SRC]
    if ids:
        return frozenset(f"{system}:{x}" for x in ids if console is None or not console(system, x))
    t = str(talker or "").strip()
    return frozenset({t}) if t and not GENERIC_TALKER.fullmatch(t) else frozenset()


def _ties_with(ties: frozenset, others) -> bool:
    """One talker: a row's ties and one of `others` (another row's), both known, and every radio of one of the two
    rows keyed up in the other's ("a,b" ties a row of a alone; never a row of "c,b": a and c are two talkers that one
    radio, a console nothing labels, answered in both)."""
    return bool(ties) and any(o and (ties <= o or o <= ties) for o in others)


def _unit_placeholder(unit: str) -> bool:
    """A department's placeholder unit (DPT239 ...: sizeup/cad/mapview.is_placeholder, a CAD unit placeholder): never a
    tie between two rows. Talkers tie by radio ID (_talker_ties), within window_s of air."""
    w = str(unit or "").strip()
    if not w:
        return True
    try:
        from sizeup.cad.mapview import is_placeholder
        return is_placeholder(w.upper())
    except Exception:  # noqa: BLE001 - the lists unreadable: not one
        return False


def _same(a: Event, b: Event) -> bool:
    keys = ("what_score", "level", "rules", "traffic_ids", "mode", "priority", "precision", "lat", "lon", "label_full",
            "confirmed", "status", "category", "near_incident", "suppressed", "fd")
    return all(getattr(a, k) == getattr(b, k) for k in keys)


def _takes_done(n) -> bool:
    """The notifier's send() takes a done callback (sizeup/notify.py Notifier / KindNotifier; not every test fake)."""
    try:
        return "done" in inspect.signature(n.send).parameters
    except (TypeError, ValueError):
        return False


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
                # never in the body: a presigned URL is ~1.5 KB (the STS token) and would flood the notification. The
                # tap plays it, and a "Listen" button; the hub's page (the click it replaces) on an "Open" button.
                # ntfy buttons are https only (notify.publish): an http hub (a test one) keeps the tap
                actions = [{"label": "Listen", "url": url}]
                hub = m.get("click") or ""
                if not hub or urlsplit(hub).scheme == "https":
                    m["click"] = url
                    if hub:
                        actions.append({"label": "Open", "url": hub})
        kw = {"actions": actions} if actions else {}
        if _takes_done(n):                          # ntfy's answer recorded on the event (events.py delivered())
            kw["done"] = self._delivered(ev.id)
        if not n.send(m["title"], m["body"], m["priority"], m["tags"], m["click"], **kw):
            self.engine.log(f"intel: event {ev.id}: no pre-alert topic in effect (or the queue is full): not pushed")
            return False
        return True

    def _delivered(self, eid: int | None):
        """The notify thread's outcome of this push onto the event: sent (ntfy's id, the HTTP status) or not (why)."""
        events, log = self.engine.events, self.engine.log

        def done(outcome: dict) -> None:
            if eid is None or not hasattr(events, "delivered"):
                return
            try:
                events.delivered(eid, outcome)
            except Exception as e:  # noqa: BLE001 - the push went (or not) either way
                log(f"intel: event {eid}: delivery not recorded ({type(e).__name__})")
        return done

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


def _block_of(label: str) -> str:
    """"100 block of Example Rd, Town" as an address of that block ("100 Example Rd, Town"); else the label."""
    return re.sub(r"^\s*(\d+)\s+block\s+of\s+", r"\1 ", label or "", flags=re.I)


def place_town(label: str) -> str:
    """The county town a normalized address ends with ("12 Example Rd, Port Chester" -> "port chester"); "" if none."""
    from sizeup.cad.sources import county_towns
    low = str(label or "").lower().replace("_", " ")
    ws = tuple(re.findall(r"[a-z0-9]+", low.rsplit(",", 1)[1] if "," in low else low))
    for k in sorted((k for k in county_towns() if k), key=len, reverse=True):
        if len(ws) >= len(k) and ws[-len(k):] == k:
            return " ".join(k)
    return ""


def fire_place(ev: Event, inc: dict, radius_m: float) -> bool:
    """The event's place is the incident's (_fire_incident): the same address (sizeup/cad/sources.py same_place), the
    same block of the same street (the house numbers' hundreds; within radius_m too when both have a point), or, for
    a confirmed address or intersection, within radius_m and half the place's radius. Never two known towns."""
    from sizeup.cad.sources import _same_street, place_key
    from sizeup.cad.sources import same_place as same_address
    mine = _block_of(ev.label_full)
    shown = str(inc.get("address") or inc.get("label") or "")
    ta, tb = (ev.town or place_town(ev.label_full)).lower(), place_town(shown) or place_town(inc.get("address_raw") or "")
    if ta and tb and ta != tb:
        return False
    near = None
    if ev.lat is not None and ev.lon is not None and inc.get("lat") is not None and inc.get("lon") is not None:
        near = _m(ev.lat, ev.lon, float(inc["lat"]), float(inc["lon"])) <= radius_m + float(ev.radius_m or 0.0) / 2
    if mine:
        ka = place_key(mine)
        for t in [x for x in (str(inc.get("address_raw") or ""), shown) if x]:
            if same_address(mine, t):
                return True
            kb = place_key(t)
            if (ka and kb and ka[0] == kb[0] == "addr" and _same_street(ka[2:], kb[2:]) and near is not False
                    and _hundreds(ka[1]) is not None and _hundreds(ka[1]) == _hundreds(kb[1])):
                return True
    return bool(near) and ev.confirmed and ev.precision in ("address", "intersection")


def _hundreds(num: str) -> int | None:
    m = re.match(r"\d+", num or "")
    return int(m.group(0)) // 100 if m else None


def fire_street_said(inc: dict, texts, said=frozenset(), own=frozenset()) -> bool:
    """An event with no place: the incident's street (its name words, then its suffix: "Example Road"; a street with no
    suffix never, too loose), or one of its intersection's, said in the words, in the incident's town: never when the
    words name another town (said: towns_said), and only when they name its town or it is one of the source's (own)."""
    from sizeup.cad.sources import ABBR, place_key
    shown = str(inc.get("address") or inc.get("label") or "")
    town = place_town(shown) or place_town(inc.get("address_raw") or "")
    if not town or (said and town not in said) or (town not in said and town not in own):
        return False
    k = place_key(str(inc.get("address_raw") or shown))
    if k is None:
        return False
    streets = [k[2:]] if k[0] == "addr" else list(k[1]) if k[0] == "x" else []
    streets = [(st[1], st[2]) for st in streets if st[1] and st[2]]          # (suffix, name words)
    for text in texts or ():
        ws = [ABBR.get(w, w) for w in re.findall(r"[a-z0-9]+", str(text).lower())]
        for suffix, name in streets:
            n = len(name)
            for i in range(len(ws) - n):
                if ws[i + n] == suffix and set(ws[i:i + n]) == set(name):
                    return True
    return False
