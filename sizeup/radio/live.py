"""Who is transmitting right now, for the "talking" highlight and the talk alerts.

P25 (county system): trunk-recorder's own log, read line by line as sizeup drains its output
(radio/recorder.py). Per call (trunk-recorder's call number, "8C") on a talkgroup:

    Starting P25 Recorder ...                       the talkgroup is up (talker not known yet)
    Unit ID set via Control Channel, ext: 41228     a new transmission on the call: radio 41228 keyed
    New MotoP25_TDMA alias: 41228 (77A4)            radio 41228's alias (first time this run): also the
                                                    talker when the call has none yet
    Concluding Recorded Call / Stopping P25 ...     the call is over
    - Transmission src: 41228 (77A4) pos: ...       who talked, listed after the call ends: a radio not
                                                    shown live is flashed as "just transmitted"

trunk-recorder logs the talker of a call's first transmission only when its alias is new (3-4 s
in, when the voice frames carry it) and a later talker only when its recorder was idle, so from the
log alone a talker shows ~4 s after the key-up, or only when the call ends (replay of the station's
log, 2026-10-08: median 3.9 s on Fire/EMS 15, a third named only at the end). Its unit_script
plugin closes that gap: sizeup turns it on in the generated config.runtime.json (radio/recorder.py,
"unitScript": <abs path>/unit-event.sh on each system) and trunk-recorder 5.2 then runs, for every
voice grant and every grant whose source differs from the call's current one (= a new key-up),

    unit-event.sh <shortName> <radioID> call <talkgroup> [patched talkgroups]

on every talkgroup of the system, recorded or not; the script prints "SIZEUP-UNIT westchester 41228
call 307" to trunk-recorder's stdout, which sizeup already reads line by line (one atomic write, no
buffering, no polling). That grant names the talker at once (on_change -> the pages and the hub
link are poked in the same call). A grant before its call's "Starting P25 Recorder" line (the script
runs in the background) or on a talkgroup nothing records is held as a call of its own: adopted by
the call's "Starting" line, else gone GRANT_IDLE_S after the grant. The finished call's JSON
(radio/p25.py) stays the record of who said what; a talker shown live is not flashed again from the
"Transmission src" lines at the end.

Every grant is also kept for GRANT_KEEP_S (grants(): time, radio ID per system and talkgroup), and
the finished call's turns are checked against them (radio/p25.py apply_grants): a key-up the call
JSON has no radio ID for is named by the grant that opened it, and a transmission that ran into the
next talker's (no terminator decoded) is cut where that talker was granted.

FG1 (analog): the app's watcher calls fm_open while the squelch is open, fm_ids when the MDC-1200
ID decodes from the audio so far (APX radios send it 0.3-0.8 s in), and fm_segment with the IDs
found in the whole transmission when it ends (a post-ID on release is only known then).
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Callable

ANSI = re.compile(r"\x1b\[[0-9;]*m")
TS = re.compile(r"^\[(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?)\]")   # trunk-recorder's (local) time
LINE = re.compile(r"\[(?P<sys>[^\]\s]+)\]\s+(?P<call>\w+)\s+TG:\s*(?P<tg>\d+)\s+Freq:\s*[\d.]+\s*MHz\s+(?P<msg>.*)$")
UNIT_SET = re.compile(r"Unit ID set via Control Channel, ext: (?P<src>\d+)")
# 5.2.0 (error level) / later (debug): a grant to another radio while the recorder holds this call's
# transmission; trunk-recorder caches it for the next transmission. Kept as a grant (call turns only).
GRANT_SET = re.compile(r"Unit ID externally set from GRANT: (?P<src>\d+)")
ALIAS = re.compile(r"New \S+ alias: (?P<src>\d+) \((?P<alias>[^)]*)\)")
SRC = re.compile(r"- Transmission src: (?P<src>-?\d+)(?: \((?P<alias>[^)]*)\))? pos:")
UNIT_TAG = "SIZEUP-UNIT "
UNIT_SCRIPT = re.compile(r"^SIZEUP-UNIT (?P<sys>\S+) (?P<src>-?\d+) (?P<ev>\w+)(?: (?P<tg>\d+))?")
CALL_IDLE_S = 30.0       # a call with no log line this long is gone (trunk-recorder restarted mid-call)
RECENT_S = 4.0           # "just transmitted": shown this long after the transmission ended
GRANT_S = 3.0            # a grant held on its own is adopted by a "Starting" line for its talkgroup this soon
GRANT_IDLE_S = 5.0       # ... and is gone this long after its last grant when no call line ever came
CONCLUDED_KEEP_S = 5.0   # the "Transmission src" lines follow the conclusion
GRANT_KEEP_S = 1800.0    # grants kept this long for the finished calls' turns (radio/p25.py apply_grants)
GRANT_DUP_S = 2.0        # the unit script and the log line naming the same grant: kept once


def _noop(*_a) -> None:
    pass


def _tr_time(line: str) -> float | None:
    """The time trunk-recorder stamped on a log line (epoch seconds), None if it has none."""
    m = TS.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m["ts"][:19], "%Y-%m-%d %H:%M:%S")) + float("0" + m["ts"][19:])
    except (ValueError, OverflowError):
        return None


class LiveTalk:
    def __init__(self, resolve: Callable[[str, str, str], dict | None] = lambda s, r, a: None,
                 tg_name: Callable[[int], str] = lambda tg: f"TG {tg}",
                 learn: Callable[[str, str, float], None] = _noop, clock: Callable[[], float] = time.time,
                 on_change: Callable[[], None] = _noop, main: Callable[[int], bool] = lambda tg: True,
                 debug: Callable[[str], None] | None = None):
        self.resolve, self.tg_name, self.learn, self.clock = resolve, tg_name, learn, clock
        self.main = main        # talkgroups shown in Radio traffic (an unidentified talker is shown only there)
        self.on_change = on_change
        self.debug = debug      # one line per talker named on a main talkgroup: how fast it went out
        self._lock = threading.Lock()
        # (system, call) -> {tg, since, src, alias, talk_since, seen, shown}; a grant with no call
        # line yet is held under (system, "grant:<tg>") with grant=True.
        self._calls: dict[tuple[str, str], dict] = {}
        self._done: dict[tuple[str, str], dict] = {}        # concluded, waiting for their "Transmission src" lines
        self._named: list[tuple[dict, str]] = []            # talkers named by the line being fed (for debug)
        self._fm: dict[str, dict] = {}                      # channel -> {since, ids}
        self._fm_last: dict[str, dict] = {}
        self._recent: list[dict] = []
        self.aliases: dict[str, str] = {}                   # P25 radio ID -> alias, learned from the log
        self._grant_log: deque[tuple[float, str, int, str]] = deque(maxlen=20000)   # (time, system, tg, src)

    # -- P25 -------------------------------------------------------------------------------------

    def feed_line(self, line: str) -> bool:
        """One line of trunk-recorder output (its log, or a unit-script line). True when the live
        state changed; on_change() has then been called before this returns."""
        got = time.time()
        line = ANSI.sub("", line).strip()
        # The unit script writes to the same pipe as trunk-recorder's log, whose message and newline
        # are two writes: a grant can land between them ("...QPSK: trueSIZEUP-UNIT ...").
        i = line.find(UNIT_TAG)
        parts = [line] if i <= 0 else [line[:i], line[i:]]
        self._named = []
        changed = False
        for part in parts:
            changed = self._feed(part, self.clock()) or changed
        if changed:
            self.on_change()
            if self.debug is not None:
                self._report(got, _tr_time(line))
        return changed

    def _feed(self, line: str, now: float) -> bool:
        if m := UNIT_SCRIPT.match(line):
            if m["ev"] == "call" and m["tg"] and m["src"] not in ("-1", "0"):
                return self._grant(m["sys"], m["src"], int(m["tg"]), now)
            return False
        if m := LINE.search(line):
            return self._call_line(m["sys"], m["call"], int(m["tg"]), m["msg"], now)
        return False

    def _report(self, got: float, stamped: float | None) -> None:
        sent = time.time()
        for call, via in self._named:
            tg = call["tg"]
            try:
                if not self.main(tg):
                    continue
                e = self._entry("", "", tg, call["src"], call["alias"], call["since"], call["talk_since"])
            except Exception:
                continue
            who = e["rig"] or e["alias"] or "unknown radio"
            t0 = stamped if stamped is not None else got
            self.debug(f"live p25: {e['channel']} (TG {tg}) radio {call['src']} ({who}) talking via {via}: "
                       f"published {(sent - t0) * 1000:.0f} ms after the {'log line' if stamped else 'grant'}, "
                       f"{max(0.0, call['talk_since'] - call['since']):.1f} s into the call")

    def grants(self, system: str, tg: int, t0: float, t1: float) -> list[tuple[float, str]]:
        """(time, radio ID) of each grant on talkgroup tg between t0 and t1 ('' system: any)."""
        with self._lock:
            return [(at, src) for at, s, g, src in self._grant_log
                    if g == tg and (not system or s == system) and t0 <= at <= t1]

    def _log_grant(self, system: str, tg: int, src: str, now: float) -> None:
        """Under the lock: one grant for the call turns. The same radio granted on the same talkgroup
        again within GRANT_DUP_S (the unit script and trunk-recorder's own log line) is one grant."""
        log = self._grant_log
        while log and now - log[0][0] > GRANT_KEEP_S:
            log.popleft()
        for at, s, g, r in reversed(log):
            if now - at > GRANT_DUP_S:
                break
            if s == system and g == tg:
                if r == src:
                    return
                break
        log.append((now, system, tg, src))

    def _grant(self, system: str, src: str, tg: int, now: float) -> bool:
        """A voice grant (or a grant with a new source on a call in progress): src is talking on tg now."""
        with self._lock:
            self._log_grant(system, tg, src, now)
            live = [c for (s, _k), c in self._calls.items() if s == system and c["tg"] == tg]
            call = max((c for c in live if not c.get("grant")), key=lambda c: c["since"], default=None) \
                or next((c for c in live if c.get("grant")), None)
            if call is None:
                call = self._calls[(system, f"grant:{tg}")] = {
                    "tg": tg, "since": now, "src": "", "alias": "", "talk_since": now, "seen": now,
                    "shown": set(), "grant": True}
            if call.get("grant"):
                call["seen"] = now
            return self._talker(call, src, now, "grant")

    def _talker(self, call: dict, src: str, now: float, via: str) -> bool:
        if call["src"] == src:
            call["seen"] = now
            return False
        call.update(src=src, alias=self.aliases.get(src, ""), talk_since=now, seen=now)
        call["shown"].add(src)
        self._named.append((call, via))
        return True

    def _open(self, system: str, key: str, tg: int, now: float) -> dict:
        """A call trunk-recorder logs for the first time: it takes over a grant held for its talkgroup."""
        g = self._calls.pop((system, f"grant:{tg}"), None)
        call = {"tg": tg, "since": now, "src": "", "alias": "", "talk_since": now, "seen": now, "shown": set()}
        if g is not None and now - g["seen"] <= GRANT_S:
            call.update(since=g["since"], src=g["src"], alias=g["alias"], talk_since=g["talk_since"],
                        shown=g["shown"])
        self._calls[(system, key)] = call
        return call

    def _call_line(self, system: str, key: str, tg: int, msg: str, now: float) -> bool:
        k = (system, key)
        with self._lock:
            if msg.startswith("Starting P25 Recorder"):
                self._open(system, key, tg, now)
                return True
            if msg.startswith("Concluding Recorded Call") or msg.startswith("Stopping P25 Recorder"):
                call = self._calls.pop(k, None)
                if call is not None:
                    self._done[k] = dict(call, ended=now)
                return call is not None
            if m := SRC.search(msg):
                src, alias = m["src"], (m["alias"] or "").strip()
                if src in ("-1", "0"):
                    return False
                if alias:
                    self.aliases[src] = alias
                    self.learn(src, alias, now)
                done = self._done.get(k)
                if done is not None and src not in done["shown"]:
                    done["shown"].add(src)          # a talker never shown live: flash it now
                    self._recent.append(self._entry(system, key, done["tg"], src, alias, done["since"], now,
                                                    ended=True))
                    return True
                return False
            call = self._calls.get(k)
            if call is None:                         # a line for a call started before we were listening
                call = self._open(system, key, tg, now)
            call["seen"] = now
            if m := GRANT_SET.search(msg):
                if m["src"] not in ("-1", "0"):
                    self._log_grant(system, tg, m["src"], now)
                return False
            if m := UNIT_SET.search(msg):
                if m["src"] not in ("-1", "0"):
                    self._log_grant(system, tg, m["src"], now)
                return self._talker(call, m["src"], now, "log")
            if m := ALIAS.search(msg):
                src, alias = m["src"], m["alias"].strip()
                self.aliases[src] = alias
                self.learn(src, alias, now)
                if not call["src"]:
                    return self._talker(call, src, now, "alias")
                if call["src"] == src and call["alias"] != alias:
                    call["alias"] = alias
                    return True
            return False

    def _entry(self, system: str, key: str, tg: int, src: str, alias: str, since: float, talk_since: float,
               ended: bool = False) -> dict:
        alias = alias or self.aliases.get(src, "")
        r = self.resolve("p25", src, alias) if src else None
        return {"key": f"p25:{system}:{key}:{src}", "system": "p25", "channel": self.tg_name(tg), "tg": tg,
                "radio_id": src, "alias": alias, "rig": (r or {}).get("unit", ""), "label": (r or {}).get("label", ""),
                "since": since, "talk_since": talk_since, "ended": ended,
                "until": talk_since + RECENT_S if ended else None,
                "dispatcher": bool(re.search(r"\bCtrl\b|\bControl\b", alias or "")), "main": bool(self.main(tg))}

    # -- FG1 (analog) ------------------------------------------------------------------------------

    def fm_open(self, channel: str, since: float) -> None:
        with self._lock:
            cur = self._fm.get(channel)
            if cur and abs(cur["since"] - since) < 0.01:
                return
            self._fm[channel] = {"since": since, "ids": []}
        self.on_change()

    def fm_ids(self, channel: str, ids: list[str]) -> None:
        with self._lock:
            cur = self._fm.get(channel)
            if cur is None or cur["ids"] == ids:
                return
            cur["ids"] = list(ids)
            cur["id_at"] = self.clock()
        self.on_change()

    def fm_close(self, channel: str) -> None:
        with self._lock:
            cur = self._fm.pop(channel, None)
            if cur is not None:
                self._fm_last[channel] = dict(cur, ended=self.clock())
        if cur is not None:
            self.on_change()

    def fm_segment(self, channel: str, ids: list[str], start: float, end: float) -> None:
        """A transmission ended with these MDC IDs. One the live decode didn't show is flashed now."""
        now = self.clock()
        with self._lock:
            cur = self._fm.pop(channel, None)
            if cur is not None:
                self._fm_last[channel] = dict(cur, ended=now)
            last = self._fm_last.get(channel) or {}
            shown = set(last.get("ids") or []) if last and last.get("since", 0) <= start + 1.0 else set()
            new = [i for i in ids if i not in shown]
            for i in new:
                self._recent.append(self._fm_entry(channel, i, start, now, ended=True))
        if cur is not None or new:
            self.on_change()

    def _fm_entry(self, channel: str, rid: str, since: float, talk_since: float, ended: bool = False) -> dict:
        """id_on_release: the carrier is open and no MDC ID yet. PCFD's APX radios send theirs after the
        PTT is let go (a post-ID: 6301 comes at the end), so FG1 says "ID on release", never a guess."""
        r = self.resolve("mdc", rid, "") if rid else None
        return {"key": f"fm:{channel}:{since:.2f}:{rid}", "system": "fm", "channel": channel, "tg": None,
                "radio_id": rid, "alias": "", "rig": (r or {}).get("unit", ""), "label": (r or {}).get("label", ""),
                "since": since, "talk_since": talk_since, "ended": ended,
                "until": talk_since + RECENT_S if ended else None, "dispatcher": False, "main": True,
                "id_on_release": not rid and not ended}

    # -- state -----------------------------------------------------------------------------------

    def state(self) -> list[dict]:
        """Everyone transmitting now (and, ended=True, who just finished and wasn't shown live),
        newest first. rig '' = a radio not on a rig, or not identified yet."""
        now = self.clock()
        out: list[dict] = []
        with self._lock:
            for k, c in list(self._calls.items()):
                if now - c["seen"] > (GRANT_IDLE_S if c.get("grant") else CALL_IDLE_S):
                    del self._calls[k]
                    continue
                out.append(self._entry(k[0], k[1], c["tg"], c["src"], c["alias"], c["since"], c["talk_since"]))
            for k, d in list(self._done.items()):
                if now - d["ended"] > CONCLUDED_KEEP_S:
                    del self._done[k]
            for ch, f in self._fm.items():
                if f["ids"]:
                    out += [self._fm_entry(ch, i, f["since"], f.get("id_at", f["since"])) for i in f["ids"]]
                else:
                    out.append(self._fm_entry(ch, "", f["since"], f["since"]))
            self._recent = [r for r in self._recent if r["until"] > now]
            out += [dict(r) for r in self._recent]
        return sorted(out, key=lambda e: -e["talk_since"])
