"""What the intel engine did with the transcripts it was handed, counted (sizeup/intel/engine.py).

"0 hits in 7 days" on every rule says nothing about why: no rule matched, or no transcript was ever in a pack's
scope. These say which. In memory, by the clock hour, per pack and overall ("" below):

    considered     transcripts handed to the engine (a re-transcription counts again)
    in_scope       ... in the pack's scope (overall: in any pack's)
    out_of_scope   ... not in it (overall: in no pack's): dropped, nothing read
    too_old        in scope, older than max_age_min (the age gate): history, never a pre-alert
    repeat         in scope, the same text as before (a re-record that changed nothing)
    quiet          in scope and read during quiet hours (events are made, nothing is pushed)
    matched        read, and a rule matched
    below          matched, but no category reached its threshold
    events         events raised
    pushed         pushes sent
    held           events not pushed for a reason (shadow, pushes off, quiet hours, a cap, merged, the scrub, unsent)

snapshot(now) gives each over the last 24 clock hours ("h24") and since the engine started ("start"), and when each
pack last had a transcript in scope. state() / restore() carry the hours and the last-in-scope times across a restart
or a handover (the engine keeps them in the store's meta, written with its hourly log line: never at startup);
"since start" begins again with each process.
"""

from __future__ import annotations

import threading

from sizeup import tz

KEYS = ("considered", "in_scope", "out_of_scope", "too_old", "repeat", "quiet", "matched", "below", "events", "pushed",
        "held")
HOUR_S = 3600.0
HOURS = 24
NOTHING_S = HOURS * HOUR_S             # a pack that is on with nothing in scope this long: a warning
ALL = ""                               # the overall counts' name
WORDS = (("considered", "considered"), ("in_scope", "in scope"), ("out_of_scope", "out of scope"), ("too_old", "too old"),
         ("repeat", "same text again"), ("quiet", "in quiet hours"), ("matched", "matched a rule"),
         ("below", "below threshold"), ("events", "events"), ("pushed", "pushed"), ("held", "held"))


class Counters:
    def __init__(self, clock):
        self.clock = clock
        self.started = clock()
        self.since = self.started          # counting since (earlier than started once restored)
        self._lock = threading.Lock()
        self.total: dict[str, dict[str, int]] = {}                 # name -> {key: n} since start
        self.hours: dict[str, dict[int, dict[str, int]]] = {}      # name -> {clock hour: {key: n}}
        self.last_in_scope: dict[str, float] = {}                  # pack -> when

    def add(self, now: float, key: str, names=(), overall: bool = True, n: int = 1) -> None:
        """key, n more: for each pack named, and overall."""
        h = int(now // HOUR_S)
        with self._lock:
            for name in ([ALL] if overall else []) + [x for x in names if x != ALL]:
                t = self.total.setdefault(name, {})
                t[key] = t.get(key, 0) + n
                b = self.hours.setdefault(name, {}).setdefault(h, {})
                b[key] = b.get(key, 0) + n

    def in_scope(self, now: float, at: float, names) -> None:
        with self._lock:
            for name in names:
                self.last_in_scope[name] = max(self.last_in_scope.get(name, 0.0), at or now)

    def _trim(self, now: float) -> None:
        first = int(now // HOUR_S) - HOURS + 1
        for hs in self.hours.values():
            for h in [h for h in hs if h < first]:
                del hs[h]

    def _one(self, name: str) -> dict:
        h24 = dict.fromkeys(KEYS, 0)
        for b in self.hours.get(name, {}).values():
            for k, v in b.items():
                if k in h24:
                    h24[k] += v
        return {"h24": h24, "start": {k: self.total.get(name, {}).get(k, 0) for k in KEYS}}

    def snapshot(self, now: float, names=()) -> dict:
        """{started, since, all: {h24, start}, packs: {name: {h24, start, last_in_scope}}} (names: the packs to show)."""
        with self._lock:
            self._trim(now)
            return {"started": self.started, "since": self.since, "all": self._one(ALL),
                    "packs": {n: {**self._one(n), "last_in_scope": self.last_in_scope.get(n)} for n in names}}

    def hour_line(self, last: dict) -> tuple[str, dict]:
        """The hourly log line over what was counted since `last` (the overall totals then), and the totals now."""
        with self._lock:
            t = dict(self.total.get(ALL, {}))
        d = {k: t.get(k, 0) - last.get(k, 0) for k in ("considered", "in_scope", "matched", "events")}
        return (f"intel: considered {d['considered']}, in scope {d['in_scope']}, matched {d['matched']}, "
                f"events {d['events']}"), t

    # -- across a restart or a handover ----------------------------------------------------------------------------

    def state(self, now: float) -> dict:
        with self._lock:
            self._trim(now)
            return {"since": self.since, "last_in_scope": dict(self.last_in_scope),
                    "hours": {n: {str(h): dict(b) for h, b in hs.items()} for n, hs in self.hours.items()}}

    def restore(self, d, now: float) -> None:
        """What was kept, under what is counted here: the larger of the two for each number, so the same state read
        twice (a leader that was a standby for a minute) adds nothing."""
        if not isinstance(d, dict):
            return
        with self._lock:
            since = d.get("since")
            if isinstance(since, (int, float)) and not isinstance(since, bool) and 0 < since < self.since:
                self.since = float(since)
            for n, at in (d.get("last_in_scope") or {}).items() if isinstance(d.get("last_in_scope"), dict) else []:
                if isinstance(at, (int, float)) and not isinstance(at, bool) and 0 < at <= now:
                    self.last_in_scope[str(n)] = max(self.last_in_scope.get(str(n), 0.0), float(at))
            for n, hs in (d.get("hours") or {}).items() if isinstance(d.get("hours"), dict) else []:
                for h, b in hs.items() if isinstance(hs, dict) else []:
                    try:
                        h = int(h)
                    except (TypeError, ValueError):
                        continue
                    mine = self.hours.setdefault(str(n), {}).setdefault(h, {})
                    for k, v in b.items() if isinstance(b, dict) else []:
                        if k in KEYS and isinstance(v, int) and not isinstance(v, bool) and v > 0:
                            mine[k] = max(mine.get(k, 0), v)
            self._trim(now)


def _when(at: float | None, now: float) -> str:
    if not at:
        return "never"
    return tz.hhmm(at) if now - at < 20 * HOUR_S else tz.clock(at, "%b %-d %H:%M")


def row(pack: dict, on: bool, since: float, started: float, now: float, label: str = "", name: str = "") -> dict:
    """One pack's terse line for Settings > System > Intel and Settings > Station > Health:
    {pack, label, on, level (ok | warn | off), text, title, in_scope, considered, last_in_scope}.
    pack: its snapshot {h24, start, last_in_scope}. warn: on, and nothing in scope for NOTHING_S (counted that long)."""
    h24, last = pack["h24"], pack.get("last_in_scope")
    quiet_for = now - max(last or 0.0, since)
    level = "off" if not on else "warn" if quiet_for >= NOTHING_S else "ok"
    text = f"{h24['in_scope']:,} in scope / {h24['considered']:,} considered · last in scope: {_when(last, now)}"
    if not on:
        text += " · off"
    part = lambda d: " · ".join(f"{d[k]:,} {w}" for k, w in WORDS)      # noqa: E731
    title = (f"24 h: {part(h24)}\nSince start ({_when(started, now)}): {part(pack['start'])}\n"
             "In scope: the transcripts this pack reads (its service, source, tier and system). Out of scope: dropped "
             "unread. Too old: past max age min. Held: not pushed (shadow, pushes off, quiet hours, a cap, merged, "
             "the scrub, unsent)."
             + ("\nNothing in scope for 24 h: no rule can match. Check the pack's scope against the sources recorded."
                if level == "warn" else ""))
    return {"pack": name, "label": label or name, "on": on, "level": level, "text": text, "title": title,
            "in_scope": h24["in_scope"], "considered": h24["considered"], "last_in_scope": last}
