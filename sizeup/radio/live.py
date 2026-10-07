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

trunk-recorder logs no unit for a call's first transmission (only when the alias is new), so a
one-transmission call shows its talker when it ends. Its unit_script plugin closes that gap: with
"unitScript": "./unit-event.sh" on the system in its config.json, each grant prints
"SIZEUP-UNIT westchester 41228 call 307" (deploy/trunk-recorder/unit-event.sh), read here too.

FG1 (analog): the app's watcher calls fm_open while the squelch is open, fm_ids when the MDC-1200
ID decodes from the audio so far (APX radios send it 0.3-0.8 s in), and fm_segment with the IDs
found in the whole transmission when it ends (a post-ID on release is only known then).
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LINE = re.compile(r"\[(?P<sys>[^\]\s]+)\]\s+(?P<call>\w+)\s+TG:\s*(?P<tg>\d+)\s+Freq:\s*[\d.]+\s*MHz\s+(?P<msg>.*)$")
UNIT_SET = re.compile(r"Unit ID set via Control Channel, ext: (?P<src>\d+)")
ALIAS = re.compile(r"New \S+ alias: (?P<src>\d+) \((?P<alias>[^)]*)\)")
SRC = re.compile(r"- Transmission src: (?P<src>-?\d+)(?: \((?P<alias>[^)]*)\))? pos:")
UNIT_SCRIPT = re.compile(r"^SIZEUP-UNIT (?P<sys>\S+) (?P<src>\d+) (?P<ev>\w+)(?: (?P<tg>\d+))?")
CALL_IDLE_S = 30.0       # a call with no log line this long is gone (trunk-recorder restarted mid-call)
RECENT_S = 4.0           # "just transmitted": shown this long after the transmission ended
GRANT_S = 3.0            # a unit-script grant waits this long for its call's "Starting" line
CONCLUDED_KEEP_S = 5.0   # the "Transmission src" lines follow the conclusion


def _noop(*_a) -> None:
    pass


class LiveTalk:
    def __init__(self, resolve: Callable[[str, str, str], dict | None] = lambda s, r, a: None,
                 tg_name: Callable[[int], str] = lambda tg: f"TG {tg}",
                 learn: Callable[[str, str, float], None] = _noop, clock: Callable[[], float] = time.time,
                 on_change: Callable[[], None] = _noop, main: Callable[[int], bool] = lambda tg: True):
        self.resolve, self.tg_name, self.learn, self.clock = resolve, tg_name, learn, clock
        self.main = main        # talkgroups shown in Radio traffic (an unidentified talker is shown only there)
        self.on_change = on_change
        self._lock = threading.Lock()
        self._calls: dict[tuple[str, str], dict] = {}       # (system, call) -> {tg, since, src, alias, talk_since, seen, shown}
        self._done: dict[tuple[str, str], dict] = {}        # concluded, waiting for their "Transmission src" lines
        self._grants: dict[tuple[str, int], tuple[str, float]] = {}
        self._fm: dict[str, dict] = {}                      # channel -> {since, ids}
        self._fm_last: dict[str, dict] = {}
        self._recent: list[dict] = []
        self.aliases: dict[str, str] = {}                   # P25 radio ID -> alias, learned from the log

    # -- P25 -------------------------------------------------------------------------------------

    def feed_line(self, line: str) -> bool:
        """One line of trunk-recorder output. True when the live state changed."""
        line = ANSI.sub("", line).strip()
        now = self.clock()
        changed = False
        if m := UNIT_SCRIPT.match(line):
            if m["ev"] == "call" and m["tg"]:
                changed = self._grant(m["sys"], m["src"], int(m["tg"]), now)
        elif m := LINE.search(line):
            changed = self._call_line(m["sys"], m["call"], int(m["tg"]), m["msg"], now)
        if changed:
            self.on_change()
        return changed

    def _grant(self, system: str, src: str, tg: int, now: float) -> bool:
        with self._lock:
            call = next((c for (s, _k), c in self._calls.items() if s == system and c["tg"] == tg), None)
            if call is None:
                self._grants[(system, tg)] = (src, now)
                return False
            return self._talker(call, src, now)

    def _talker(self, call: dict, src: str, now: float) -> bool:
        if call["src"] == src:
            call["seen"] = now
            return False
        call.update(src=src, alias=self.aliases.get(src, ""), talk_since=now, seen=now)
        call["shown"].add(src)
        return True

    def _call_line(self, system: str, key: str, tg: int, msg: str, now: float) -> bool:
        k = (system, key)
        with self._lock:
            if msg.startswith("Starting P25 Recorder"):
                g = self._grants.pop((system, tg), None)
                self._calls[k] = {"tg": tg, "since": now, "src": "", "alias": "", "talk_since": now, "seen": now,
                                  "shown": set()}
                if g and now - g[1] <= GRANT_S:
                    self._talker(self._calls[k], g[0], now)
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
                call = self._calls[k] = {"tg": tg, "since": now, "src": "", "alias": "", "talk_since": now,
                                         "seen": now, "shown": set()}
            call["seen"] = now
            if m := UNIT_SET.search(msg):
                return self._talker(call, m["src"], now)
            if m := ALIAS.search(msg):
                src, alias = m["src"], m["alias"].strip()
                self.aliases[src] = alias
                self.learn(src, alias, now)
                if not call["src"]:
                    return self._talker(call, src, now)
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
        r = self.resolve("mdc", rid, "") if rid else None
        return {"key": f"fm:{channel}:{since:.2f}:{rid}", "system": "fm", "channel": channel, "tg": None,
                "radio_id": rid, "alias": "", "rig": (r or {}).get("unit", ""), "label": (r or {}).get("label", ""),
                "since": since, "talk_since": talk_since, "ended": ended,
                "until": talk_since + RECENT_S if ended else None, "dispatcher": False, "main": True}

    # -- state -----------------------------------------------------------------------------------

    def state(self) -> list[dict]:
        """Everyone transmitting now (and, ended=True, who just finished and wasn't shown live),
        newest first. rig '' = a radio not on a rig, or not identified yet."""
        now = self.clock()
        out: list[dict] = []
        with self._lock:
            for k, c in list(self._calls.items()):
                if now - c["seen"] > CALL_IDLE_S:
                    del self._calls[k]
                    continue
                out.append(self._entry(k[0], k[1], c["tg"], c["src"], c["alias"], c["since"], c["talk_since"]))
            for k, d in list(self._done.items()):
                if now - d["ended"] > CONCLUDED_KEEP_S:
                    del self._done[k]
            for (s, tg), (_src, at) in list(self._grants.items()):
                if now - at > GRANT_S:
                    del self._grants[(s, tg)]
            for ch, f in self._fm.items():
                if f["ids"]:
                    out += [self._fm_entry(ch, i, f["since"], f.get("id_at", f["since"])) for i in f["ids"]]
                else:
                    out.append(self._fm_entry(ch, "", f["since"], f["since"]))
            self._recent = [r for r in self._recent if r["until"] > now]
            out += [dict(r) for r in self._recent]
        return sorted(out, key=lambda e: -e["talk_since"])
