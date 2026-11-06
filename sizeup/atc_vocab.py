"""The sizeup-atc vocabulary kept up with the traffic (GitHub #257): the operators seen near HPN lately, by name.

Amazon Transcribe streams HPN Tower with one custom vocabulary (sizeup-atc: data/atc/vocabulary.json), and a stream
can't be given hints of its own. So the vocabulary itself follows the traffic: every EVERY_S the operators whose
aircraft the Aircraft layer showed near HPN in the last WINDOW_S (Operators, fed by sizeup/atc_talkers.py observe) are
said by name (their telephony: "Moxy", "Execjet", "Sun Country") and added to the fixed list; when that set differs
from what AWS has (the list's signature), it is pushed with the same code as Settings > Radios > Vocabulary > Push
(sizeup/transcribe.py Vocabulary: UpdateVocabulary, then GetVocabulary until READY). Flight numbers are not added:
they are digits, which Transcribe already writes, and a list of them would crowd out the names.

    rate        at most one push every EVERY_S (3 h) and never while one is PENDING (AWS takes minutes)
    streams     never interrupted: streams already open keep the vocabulary they opened with; while the new one is
                PENDING, new HPN Tower streams open without one (on_pending), and take it again once READY (the app's
                _atc_vocab_ready), so no stream asks for a vocabulary AWS is still building
    leader      only the CAD of record that holds the hub's lease (#254's fence: leader()); a standby or a station never
    switch      Settings > System > ATC talkers > "Refresh the ATC vocabulary from the traffic" (on by default)
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable

from sizeup import atc

EVERY_S = 3 * 3600.0          # how often the set is looked at (and the most often it is pushed)
WINDOW_S = 48 * 3600.0        # operators seen this recently are named
MAX_OPERATORS = 120           # at most this many names added (the list's cap is 500 phrases)
KEY = "atc_operators_seen"    # the CAD meta row that keeps them across restarts
SAVE_S = 600.0
_AIRLINE = re.compile(r"([A-Z]{3})\d{1,4}[A-Z]{0,2}$")


class Operators:
    """Designator -> when an aircraft of it was last near HPN (an airline-style callsign: "MXY283" -> MXY), kept in the
    CAD's meta table (get / put) so a restart doesn't forget the last two days."""

    def __init__(self, get: Callable[[str], str | None] = lambda k: None, put: Callable[[str, str], None] = lambda k, v: None,
                 clock: Callable[[], float] = time.time):
        self._get, self._put, self.clock = get, put, clock
        self._lock = threading.Lock()
        try:
            d = json.loads(get(KEY) or "{}")
        except (ValueError, TypeError):
            d = {}
        self.seen: dict[str, float] = {k: float(v) for k, v in d.items() if isinstance(v, (int, float))} \
            if isinstance(d, dict) else {}
        self._saved = 0.0

    def add(self, planes, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            for a in planes or ():
                m = _AIRLINE.match(str((a or {}).get("flight") or "").strip().upper())
                if m:
                    self.seen[m.group(1)] = now
            for k in [k for k, t in self.seen.items() if now - t > WINDOW_S]:
                del self.seen[k]
            if now - self._saved >= SAVE_S:
                self._saved = now
                self._put(KEY, json.dumps(self.seen, sort_keys=True))

    def recent(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        with self._lock:
            return sorted((k for k, t in self.seen.items() if now - t <= WINDOW_S), key=lambda k: -self.seen[k])


def names(designators, telephony: dict[str, str] | None = None) -> list[str]:
    """Each designator's telephony as said, written as a name ("blue streak" -> "Blue Streak"); unknown ones skipped."""
    tel = atc.telephony_words() if telephony is None else telephony
    out = []
    for d in designators:
        t = tel.get(d)
        if t and not any(c.isdigit() for c in t):
            out.append(" ".join(w.capitalize() for w in t.split()))
    return list(dict.fromkeys(out))[:MAX_OPERATORS]


class Refresher:
    """tick() now and then (each Aircraft layer picture): pushes when it is due, allowed and the set changed."""

    def __init__(self, vocab, operators: Operators, static: Callable[[], list[str]], enabled: Callable[[], bool],
                 leader: Callable[[], bool], on_pending: Callable[[], None] = lambda: None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 every_s: float = EVERY_S, background: bool = True):
        self.vocab, self.operators, self.static = vocab, operators, static
        self.enabled, self.leader, self.on_pending = enabled, leader, on_pending
        self.clock, self.log, self.every_s, self.background = clock, log, every_s, background
        self.last_look = 0.0
        self.last = ""                          # what the last look did (Settings, tests)
        self._busy = False

    def phrases(self, now: float | None = None) -> list[str]:
        base = list(self.static())
        have = {p.lower() for p in base}
        return base + [n for n in names(self.operators.recent(now)) if n.lower() not in have]

    def tick(self, now: float | None = None) -> str:
        """off | standby | not yet | pending | busy | unchanged | pushed: what happened."""
        from sizeup.transcribe import build_vocabulary
        import hashlib
        now = self.clock() if now is None else now
        if not self.enabled():
            self.last = "off"
        elif not self.leader():
            self.last = "standby"
        elif self._busy:
            self.last = "busy"
        elif now - self.last_look < self.every_s:
            self.last = "not yet"
        else:
            st = self.vocab.state()
            if st["state"] == "PENDING":
                self.last = "pending"
                return self.last
            if st.get("updated") and now - float(st["updated"]) < self.every_s:
                self.last_look = now          # pushed lately (by hand or here): the next look in every_s
                self.last = "not yet"
                return self.last
            self.last_look = now
            phrases = self.phrases(now)
            sig = hashlib.sha1(json.dumps(build_vocabulary(phrases)["phrases"]).encode()).hexdigest()[:12]
            if sig == st.get("sig"):
                self.last = "unchanged"
                return self.last
            self.last = "pushed"
            self._push(phrases)
        return self.last

    def _push(self, phrases: list[str]) -> None:
        def run():
            try:
                self.on_pending()
                st = self.vocab.push(phrases)
                self.log(f"atc vocabulary: refreshed with the operators seen near HPN ({len(phrases)} phrases, {st['state']})")
            except Exception as e:  # noqa: BLE001 - the refresh must never stop anything
                self.log(f"atc vocabulary: refresh failed: {type(e).__name__}: {e}")
            finally:
                self._busy = False
        self._busy = True
        if self.background:
            threading.Thread(target=run, name="atc-vocab", daemon=True).start()
        else:
            run()
