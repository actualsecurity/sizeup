"""Shed clips caught up (the clips speech-to-text skipped when it was busy, sizeup/stt.py SHED).

When the station's whisper is busy with fire traffic, a context clip that waits too long is shed: "Skipped: busy" on
the Radio page, still re-runnable. This runs those again once there is room, on the CAD of record (the hub, or
all-in-one), on its own thread, a few at a time:

    which       radio log rows still "skipped" for being busy (Comms.shed_clips), at most max_age old (Settings >
                Radios > Speech-to-text, 6 h by default): the alert tier first (police dispatch), then the newest
    when        their engine is idle: the station's whisper with no clip of ours (urgent, high, live) nor an alert clip
                waiting, and at most QUEUE_IDLE_MAX waiting in all; an AWS channel with nothing of its own open
    how         the existing re-run path, so the text, model, history and change log behave like any re-run: AWS on
                a context channel set to AWS while its own budget (police) has room, never ours nor its reserve;
                otherwise the local model (the station's whisper: its catch_up call), in the CATCHUP class, under
                every other clip and started only after a quiet spell with nothing of ours coming in (sizeup/stt.py)
    bounds      WHISPER_BATCH clips to whisper a tick and none more until the station says they are done,
                AWS_BATCH per channel; a clip that didn't come back transcribed is tried again after RETRY_S, at most
                MAX_TRIES times

Nothing is written here: no startup migration, no bulk write. The rows change only through the re-run path, one at
a time, as each clip finishes. The first look is START_DELAY_S after the start. stats(): the backlog waiting and the
clips caught up in the last 24 h (Settings > Health, next to the shed counts).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

TICK_S = 15.0              # how often it looks
START_DELAY_S = 300.0      # the first look, this long after the start (nothing at start)
WHISPER_BATCH = 3          # clips handed to whisper a tick, and no more until the station says they are done
AWS_BATCH = 2              # clips sent to one AWS channel a tick (that channel idle)
QUEUE_IDLE_MAX = 5         # whisper "idle": at most this many clips waiting in all (context talkgroups, re-runs)
RETRY_S = 900.0            # a clip sent that is still skipped this long after: tried again ...
MAX_TRIES = 2              # ... at most this many times in all
FETCH = 60                 # rows looked at a tick
DEFAULT_MAX_AGE_H = 6.0
MAX_AGE_RANGE_H = (0.5, 48.0)
DAY_S = 86400.0


def whisper_idle(sq: dict | None) -> tuple[bool, str]:
    """The station's whisper queue (SttQueue.stats()) idle enough for a catch-up batch: nothing of ours waiting (urgent,
    high, live), no alert-tier context clip, the last batch done, and QUEUE_IDLE_MAX at most waiting."""
    if not isinstance(sq, dict):
        return False, "no word from the speech-to-text queue"
    for k, why in (("live", "live clips waiting"), ("urgent", "fire clips waiting"), ("high", "airband clips waiting"),
                   ("context", "alert clips waiting"), ("catchup", "the last batch still waiting")):
        if int(sq.get(k) or 0) > 0:
            return False, why
    if int(sq.get("queued") or 0) > QUEUE_IDLE_MAX:
        return False, f"{sq.get('queued')} clips waiting"
    return True, ""


class CatchUp:
    """The driver. Every hook is the app's (App / HubApp wire them):
        settings()            (on, max_age_h)
        may_run()             this process is the CAD of record and may change rows (the hub's leader)
        rows(since, limit)    shed rows, the alert tier first then the newest: [{id, key, source, unit, start, ...}]
        count(since)          how many shed rows wait
        caught(since)         how many were caught up since then
        route(key)            "aws" | "whisper" | "" (not transcribed now)
        whisper_ready(after)  (ok, why): whisper idle, and its word on that taken after `after` (the last batch sent)
        aws_idle(key)         nothing of that channel's open on AWS
        to_whisper(specs)     the ids queued on whisper (the station's catch_up call)
        to_aws(row)           True once it went to AWS"""

    def __init__(self, *, settings: Callable[[], tuple[bool, float]], may_run: Callable[[], bool],
                 rows: Callable[[float, int], list[dict]], count: Callable[[float], int], caught: Callable[[float], int],
                 route: Callable[[str], str], whisper_ready: Callable[[float], tuple[bool, str]],
                 aws_idle: Callable[[str], bool], to_whisper: Callable[[list[dict]], list[str]],
                 to_aws: Callable[[dict], bool], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time):
        self.settings, self.may_run, self.rows, self.count, self.caught = settings, may_run, rows, count, caught
        self.route, self.whisper_ready, self.aws_idle = route, whisper_ready, aws_idle
        self.to_whisper, self.to_aws, self.log, self.clock = to_whisper, to_aws, log, clock
        self._tries: dict[str, tuple[int, float]] = {}      # id -> (times sent, last sent)
        self._whisper_at = 0.0                               # the last batch handed to whisper
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict = {"on": None, "waiting": None, "caught_24h": None, "last_at": None, "why": "", "sent": 0,
                            "error": ""}

    def start(self, delay_s: float = START_DELAY_S) -> CatchUp:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, args=(delay_s,), name="stt-catchup", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self, delay_s: float) -> None:
        if self._stop.wait(delay_s):
            return
        while True:
            try:
                self.tick()
            except Exception as e:  # never stops: the next tick tries again
                self.state["error"] = f"{type(e).__name__}: {e}"[:200]
                self.log(f"speech-to-text catch-up: {self.state['error']}")
            if self._stop.wait(TICK_S):
                return

    def _ok_to_send(self, cid: str, now: float) -> bool:
        n, at = self._tries.get(cid, (0, 0.0))
        return n == 0 or (n < MAX_TRIES and now - at >= RETRY_S)

    def tick(self, now: float | None = None) -> int:
        """One look: the counts, and when there is room a few clips sent. How many were sent."""
        now = self.clock() if now is None else now
        on, max_age_h = self.settings()
        since = now - max_age_h * 3600.0
        self.state.update(on=bool(on), max_age_h=max_age_h, last_at=now, error="")
        if not self.may_run():
            self.state.update(waiting=None, caught_24h=None, why="not the CAD of record")
            return 0
        for cid in [c for c, (_n, at) in self._tries.items() if now - at > DAY_S]:
            self._tries.pop(cid, None)
        self.state["caught_24h"] = self.caught(now - DAY_S)
        self.state["waiting"] = self.count(since) if on else None
        if not on:
            self.state["why"] = "off"
            return 0
        rows = [r for r in self.rows(since, FETCH) if self._ok_to_send(str(r.get("id")), now)]
        if not rows:
            self.state["why"] = "nothing to catch up"
            return 0
        whisper: list[dict] = []
        aws: dict[str, list[dict]] = {}
        for r in rows:                                   # (in order: the alert tier first, then the newest)
            how = self.route(str(r.get("key") or ""))
            if how == "aws":
                aws.setdefault(str(r["key"]), []).append(r)
            elif how == "whisper":
                whisper.append(r)
        sent, why = 0, []
        for key, rs in aws.items():
            if not self.aws_idle(key):
                why.append(f"{key}: AWS busy")
                continue
            for r in rs[:AWS_BATCH]:
                if self.to_aws(r):
                    self._sent(str(r["id"]), now)
                    sent += 1
        if whisper:
            ok, busy = self.whisper_ready(self._whisper_at)
            if ok:
                batch = whisper[:WHISPER_BATCH]
                got = set(self.to_whisper(batch) or ())
                for r in batch:                          # one with no recording there: counted as tried
                    self._sent(str(r["id"]), now)
                sent += len(got)
                if got:
                    self._whisper_at = now
            else:
                why.append(f"whisper: {busy}")
        self.state["sent"] = self.state.get("sent", 0) + sent
        self.state["why"] = "; ".join(why)
        return sent

    def _sent(self, cid: str, now: float) -> None:
        n, _at = self._tries.get(cid, (0, 0.0))
        self._tries[cid] = (n + 1, now)

    def stats(self) -> dict:
        """Settings > Health: on, the backlog waiting, caught up in the last 24 h, why it is waiting (if it is)."""
        return {k: self.state.get(k) for k in ("on", "max_age_h", "waiting", "caught_24h", "last_at", "why", "sent",
                                               "error")}
