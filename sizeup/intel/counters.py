"""What the intel engine did with the transcripts it was handed, counted (sizeup/intel/engine.py).

"0 hits in 7 days" on every rule says nothing about why: no rule matched, or no transcript was ever in a pack's
scope. These say which. In memory, by the clock hour, per pack and overall ("" below):

    considered     transcripts handed to the engine (a re-transcription counts again)
    in_scope       ... in the pack's scope (overall: in any pack's)
    out_of_scope   ... not in it (overall: in no pack's): dropped, nothing read
    too_old        in scope, older than max_age_min (the age gate): history, never a pre-alert (read as context
                   and recorded at log, "late", up to engine.IGNORE_S; never read past that)
    repeat         in scope, the same text as before (a re-record that changed nothing)
    quiet          in scope and read during quiet hours (events are made, nothing is pushed)
    matched        read, and a rule matched (not counted for a pack that is off)
    below          matched, but no category reached its threshold
    events         events raised
    pushed         pushes sent (a push again of the same event counts again)
    held           events not pushed for a reason (shadow, pushes off, quiet hours, a cap, merged, the scrub, unsent),
                   once per event, when it was first held (one pushed later is in both)
    late           a push source's text read late (over its pack's late_s after the clip ended, or #747's flag), once per
                   row; late_<cause> by what made it (engine.late_cause: aws, gpu, station_pool, station, catchup,
                   outage, other); late_lost: its words reached a category at push (a pre-alert lost). Overall:
                   engine.late_row, Settings > Station > Health

These are counted as things happen; the Intel card's "24 h: events · pushed · held" line is read from the events kept
(sizeup/intel/events.py counts()), per event as it is now, so the two can differ.

snapshot(now, names) gives each over the last 24 clock hours ("h24") and since the engine started ("start"), and when
each pack last had a transcript in scope: when the engine was handed it, not when it was said (a catch-up of a day-old
row is in scope now). watching(now, names) says which packs are on and counted here; a pack is "watched since" the
later of that and its last transcript in scope, and row() warns once that is NOTHING_S ago.

state() / restore() carry the hours, the last-in-scope times and the watched-since times across a restart or a
handover (the engine keeps them in the store's meta, written with its hourly log line: never at startup); "since
start" begins again with each process. Only the packs named and the hours of the window are kept or taken back, and a
kept state over STATE_MAX characters is refused (the engine starts fresh).
"""

from __future__ import annotations

import threading

from sizeup import tz

KEYS = ("considered", "in_scope", "out_of_scope", "too_old", "repeat", "quiet", "matched", "below", "events", "pushed",
        "held", "late", "late_aws", "late_gpu", "late_station_pool", "late_station", "late_catchup", "late_outage",
        "late_other", "late_lost")
HOUR_S = 3600.0
HOURS = 24
NOTHING_S = HOURS * HOUR_S             # a pack that is on with nothing in scope this long: a warning
ALL = ""                               # the overall counts' name
STATE_MAX = 64_000                     # the kept state, serialised: larger is not ours (refused)
COUNT_MAX = 10 ** 9                    # no hour's count is larger
WORDS = (("considered", "considered"), ("in_scope", "in scope"), ("out_of_scope", "out of scope"), ("too_old", "too old"),
         ("repeat", "same text again"), ("quiet", "in quiet hours"), ("matched", "matched a rule"),
         ("below", "below threshold"), ("events", "events"), ("pushed", "pushed"), ("held", "held"))


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Counters:
    def __init__(self, clock):
        self.clock = clock
        self.started = clock()
        self._lock = threading.Lock()
        self.total: dict[str, dict[str, int]] = {}                 # name -> {key: n} since start
        self.hours: dict[str, dict[int, dict[str, int]]] = {}      # name -> {clock hour: {key: n}}
        self.last_in_scope: dict[str, float] = {}                  # pack -> when the engine last had one in scope
        self.on_since: dict[str, float] = {}                       # pack -> on and counted here since (watching())

    def add(self, now: float, key: str, names=(), overall: bool = True, n: int = 1) -> None:
        """key, n more: for each pack named, and overall."""
        h = int(now // HOUR_S)
        with self._lock:
            for name in ([ALL] if overall else []) + [x for x in names if x != ALL]:
                t = self.total.setdefault(name, {})
                t[key] = t.get(key, 0) + n
                b = self.hours.setdefault(name, {}).setdefault(h, {})
                b[key] = b.get(key, 0) + n

    def in_scope(self, now: float, names) -> None:
        with self._lock:
            for name in names:
                self.last_in_scope[name] = max(self.last_in_scope.get(name, 0.0), now)

    def watching(self, now: float, names) -> None:
        """The packs that are on and counted here, as of now: one newly so is watched from now, one no longer so (off,
        detection off, this engine not the one that counts) is not watched at all."""
        names = set(names)
        with self._lock:
            for n in [n for n in self.on_since if n not in names]:
                del self.on_since[n]
            for n in names:
                self.on_since.setdefault(n, now)

    def _trim(self, now: float) -> None:
        last = int(now // HOUR_S)
        for hs in self.hours.values():
            for h in [h for h in hs if h <= last - HOURS or h > last]:
                del hs[h]

    def _one(self, name: str) -> dict:
        h24 = dict.fromkeys(KEYS, 0)
        for b in self.hours.get(name, {}).values():
            for k, v in b.items():
                if k in h24:
                    h24[k] += v
        return {"h24": h24, "start": {k: self.total.get(name, {}).get(k, 0) for k in KEYS}}

    def snapshot(self, now: float, names=()) -> dict:
        """{started, all: {h24, start}, packs: {name: {h24, start, last_in_scope, on_since}}} (names: the packs)."""
        with self._lock:
            self._trim(now)
            return {"started": self.started, "all": self._one(ALL),
                    "packs": {n: {**self._one(n), "last_in_scope": self.last_in_scope.get(n),
                                  "on_since": self.on_since.get(n)} for n in names}}

    def hour_line(self, last: dict) -> tuple[str, dict]:
        """The hourly log line over what was counted since `last` (the overall totals then), and the totals now."""
        with self._lock:
            t = dict(self.total.get(ALL, {}))
        d = {k: t.get(k, 0) - last.get(k, 0) for k in ("considered", "in_scope", "matched", "events")}
        return (f"intel: considered {d['considered']}, in scope {d['in_scope']}, matched {d['matched']}, "
                f"events {d['events']}"), t

    # -- across a restart or a handover ----------------------------------------------------------------------------

    def state(self, now: float, names) -> dict:
        """What is kept: the window's hours of these packs (and overall), their last-in-scope and watched-since times."""
        keep = {ALL, *names}
        with self._lock:
            self._trim(now)
            return {"last_in_scope": {n: at for n, at in self.last_in_scope.items() if n in keep},
                    "on_since": {n: at for n, at in self.on_since.items() if n in keep},
                    "hours": {n: {str(h): dict(b) for h, b in hs.items()} for n, hs in self.hours.items() if n in keep}}

    def restore(self, d, now: float, names) -> None:
        """What was kept, under what is counted here: the larger of the two for each number, so the same state read
        twice (a leader that was a standby for a minute) adds nothing. Only these packs (and overall), only the
        window's hours (never a later one), only counts; a watched-since time only for a pack watched here now."""
        if not isinstance(d, dict):
            return
        keep = {ALL, *names}
        last = int(now // HOUR_S)

        def times(key):
            got = d.get(key)
            return [(str(n), float(at)) for n, at in got.items() if _num(at) and 0 < at <= now and n in keep] \
                if isinstance(got, dict) else []
        with self._lock:
            for n, at in times("last_in_scope"):
                self.last_in_scope[n] = max(self.last_in_scope.get(n, 0.0), at)
            for n, at in times("on_since"):
                if n in self.on_since:
                    self.on_since[n] = min(self.on_since[n], at)
            for n, hs in (d.get("hours") or {}).items() if isinstance(d.get("hours"), dict) else []:
                if n not in keep or not isinstance(hs, dict):
                    continue
                for h, b in hs.items():
                    try:
                        h = int(h)
                    except (TypeError, ValueError):
                        continue
                    if not last - HOURS < h <= last or not isinstance(b, dict):
                        continue
                    mine = self.hours.setdefault(n, {}).setdefault(h, {})
                    for k, v in b.items():
                        if k in KEYS and isinstance(v, int) and not isinstance(v, bool) and 0 < v <= COUNT_MAX:
                            mine[k] = max(mine.get(k, 0), v)
            self._trim(now)


def _when(at: float | None, now: float) -> str:
    if not at:
        return "never"
    return tz.hhmm(at) if now - at < 20 * HOUR_S else tz.clock(at, "%b %-d %H:%M")


def row(pack: dict, on: bool, started: float, now: float, label: str = "", name: str = "", here: bool = True) -> dict:
    """One pack's terse line for Settings > System > Intel and Settings > Station > Health:
    {pack, label, on, level (ok | warn | off), text, title, in_scope, considered, last_in_scope}.
    pack: its snapshot {h24, start, last_in_scope, on_since}. here: this engine is the one that counts (not a station
    forwarding to a hub, not a standby). warn: on, counted here, and watched NOTHING_S with nothing in scope."""
    h24, last, since = pack["h24"], pack.get("last_in_scope"), pack.get("on_since")
    watched = on and here and since is not None
    level = "off" if not watched else "warn" if now - max(last or 0.0, since) >= NOTHING_S else "ok"
    text = f"{h24['in_scope']:,} in scope / {h24['considered']:,} considered · last in scope: {_when(last, now)}"
    if not here:
        text += " · not counted here"
    elif not on:
        text += " · off"
    part = lambda d: " · ".join(f"{d[k]:,} {w}" for k, w in WORDS)      # noqa: E731
    title = (f"24 h: {part(h24)}\nSince start ({_when(started, now)}): {part(pack['start'])}\n"
             "In scope: the transcripts this pack reads (its service, source, tier and system). Out of scope: dropped "
             "unread. Too old: past max age min. Held: events not pushed (shadow, pushes off, quiet hours, a cap, "
             "merged, the scrub, unsent), once each; pushed counts a push again too. Counted as they happen, so they "
             "can differ from the card's 24 h line, which is read from the events kept."
             + ("\nNot counted here: the hub that leads reads the transcripts." if not here else "")
             + ("\nNothing in scope for 24 h: no rule can match. Check the pack's scope against the sources recorded; "
                "a source that was simply quiet for a day shows this too." if level == "warn" else ""))
    return {"pack": name, "label": label or name, "on": on, "level": level, "text": text, "title": title,
            "in_scope": h24["in_scope"], "considered": h24["considered"], "last_in_scope": last}
