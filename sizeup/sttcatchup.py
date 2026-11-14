"""Shed clips caught up (the clips speech-to-text skipped when it was busy, sizeup/stt.py SHED).

When the station's whisper is busy with fire traffic, a context clip that waits too long is shed: "Skipped: busy" on
the Radio page, still re-runnable. This runs those again once there is room, on the CAD of record (the hub, or
all-in-one), on its own thread, a few at a time:

    which       radio log rows still "skipped" for being busy (Comms.shed_clips), at most max_age old (Settings >
                Radios > Speech-to-text, 6 h by default): the alert tier first (police dispatch), then the newest.
                Rows that can't go now are sorted out before the limit (given up, not transcribed, too long for
                whisper), so the ones behind them get their turn
    fire first  nothing at all while an event of ours is open, an alarm or an emergency window is open, a carrier is
                open or someone is talking (pause); then only when the engine is idle: the station's whisper with no
                clip of ours (urgent, high, live) nor an alert clip waiting and at most QUEUE_IDLE_MAX waiting in all;
                an AWS channel with nothing of its own open
    how         the existing re-run path, quiet: the text, model, history and change log behave like any re-run, but
                nothing that alerts hears words that may be hours old (App._retext quiet). AWS on a context channel
                set to AWS while its own budget (police) has room and is under 80 % of its month, never ours nor its
                reserve; otherwise the local model (the station's whisper: its catch_up call), in the CATCHUP class:
                under every other clip, started only after a quiet spell with nothing of ours heard, one greedy pass,
                one try, a short timeout, at most MAX_CLIP_S of audio (sizeup/stt.py)
    bounds      WHISPER_BATCH clips to whisper a tick and none more until the station says they are done,
                AWS_BATCH per channel; a try that failed is on the row (rerun_result "failed: why; catch-up try N":
                it outlives a restart and a handover) and is tried again after RETRY_S, at most MAX_TRIES times; a
                call to the station that itself failed counts for nothing

Nothing is written here: no startup migration, no bulk write. The rows change only through the re-run path, one at
a time, as each clip finishes. The first look is START_DELAY_S after the start. stats(): the backlog waiting, the
clips given up on and the clips caught up in the last 24 h (Settings > Health, next to the shed counts).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from sizeup.stt import CATCHUP_MAX_CLIP_S

TICK_S = 15.0              # how often it looks
START_DELAY_S = 300.0      # the first look, this long after the start (nothing at start)
WHISPER_BATCH = 3          # clips handed to whisper a tick, and no more until the station says they are done
AWS_BATCH = 2              # clips sent to one AWS channel a tick (that channel idle)
QUEUE_IDLE_MAX = 5         # whisper "idle": at most this many clips waiting in all (context talkgroups, re-runs)
RETRY_S = 900.0            # a clip sent that is still skipped this long after: tried again ...
MAX_TRIES = 2              # ... at most this many times in all
FETCH = 60                 # eligible rows taken a tick
MAX_CLIP_S = CATCHUP_MAX_CLIP_S   # a longer clip never goes to whisper (AWS takes it, on a channel that may)
CAUGHT_CACHE_S = 60.0      # the 24 h count is read from the radio log this often
DEFAULT_MAX_AGE_H = 6.0
MAX_AGE_RANGE_H = (0.5, 48.0)
DAY_S = 86400.0
WAITING = ("", "retry later", "in flight")       # shed_clips' words for a row that still gets its turn
GAVE_UP = ("gave up", "too long")


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
        pause()               why nothing is caught up now ("" = go): fire work first
        scan(since, limit, sort)   Comms.shed_clips: (the eligible rows in order, how many rows got each word)
        caught(since)         how many were caught up since then
        route(key)            "aws" | "whisper" | "" (not transcribed now)
        whisper_ready(after)  (ok, why): whisper idle, and its word on that taken after `after` (the last batch sent)
        aws_idle(key)         nothing of that channel's open on AWS
        to_whisper(specs)     {queued: [ids], refused: {id: why}, hold: why}; None: the call itself failed
        to_aws(row)           (sent, why it never can be)
        give_up(id, why)      say on the row that it is given up (no recording, too long)"""

    def __init__(self, *, settings: Callable[[], tuple[bool, float]], may_run: Callable[[], bool],
                 pause: Callable[[], str], scan: Callable[..., tuple[list[dict], dict]], caught: Callable[[float], int],
                 route: Callable[[str], str], whisper_ready: Callable[[float], tuple[bool, str]],
                 aws_idle: Callable[[str], bool], to_whisper: Callable[[list[dict]], dict | None],
                 to_aws: Callable[[dict], tuple[bool, str]], give_up: Callable[[str, str], None],
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 max_clip_s: float = MAX_CLIP_S):
        self.settings, self.may_run, self.pause, self.scan, self.caught = settings, may_run, pause, scan, caught
        self.route, self.whisper_ready, self.aws_idle = route, whisper_ready, aws_idle
        self.to_whisper, self.to_aws, self.give_up, self.log, self.clock = to_whisper, to_aws, give_up, log, clock
        self.max_clip_s = float(max_clip_s)
        self._sent: dict[str, tuple[int, float]] = {}       # id -> (times sent by this process, last sent)
        self._whisper_at = 0.0                               # the last batch handed to whisper
        self._caught: tuple[float, int | None] = (0.0, None)  # (read at, the 24 h count)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict = {"on": None, "waiting": None, "gave_up": None, "caught_24h": None, "last_at": None,
                            "why": "", "sent": 0, "error": ""}

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

    def _sorter(self, now: float) -> Callable[[dict], str]:
        """shed_clips' sort for this tick: "" for a row to send now, else why not. The route is asked once a key."""
        routes: dict[str, str] = {}

        def sort(r: dict) -> str:
            key = str(r.get("key") or "")
            how = routes[key] if key in routes else routes.setdefault(key, self.route(key))
            r["route"] = how
            if not how:
                return "not transcribed"
            tries, tried_at = int(r.get("tries") or 0), float(r.get("tried_at") or 0.0)   # failed tries, on the row
            n, at = self._sent.get(str(r.get("id")), (0, 0.0))                             # sent, by this process
            if tries >= MAX_TRIES or (n >= MAX_TRIES and now - at >= RETRY_S):
                return "gave up"
            if how == "whisper" and float(r.get("seconds") or 0.0) > self.max_clip_s:
                return "too long"
            if n and now - at < RETRY_S:
                return "in flight" if n > tries else "retry later"
            if tries and now - tried_at < RETRY_S:
                return "retry later"
            return ""
        return sort

    def _caught_24h(self, now: float) -> int | None:
        at, n = self._caught
        if n is None or now - at >= CAUGHT_CACHE_S or now < at:
            n = self.caught(now - DAY_S)
            self._caught = (now, n)
        return n

    def tick(self, now: float | None = None) -> int:
        """One look: the counts, and when there is room a few clips sent. How many were sent."""
        now = self.clock() if now is None else now
        on, max_age_h = self.settings()
        since = now - max_age_h * 3600.0
        self.state.update(on=bool(on), max_age_h=max_age_h, last_at=now, error="")
        if not self.may_run():
            self.state.update(waiting=None, gave_up=None, caught_24h=None, why="not the CAD of record")
            return 0
        for cid in [c for c, (_n, at) in self._sent.items() if now - at > DAY_S]:
            self._sent.pop(cid, None)
        self.state["caught_24h"] = self._caught_24h(now)
        if not on:
            self.state.update(waiting=None, gave_up=None, why="off")
            return 0
        rows, counts = self.scan(since, FETCH, self._sorter(now))
        self.state["waiting"] = sum(counts.get(w, 0) for w in WAITING)
        self.state["gave_up"] = sum(counts.get(w, 0) for w in GAVE_UP)
        if not rows:
            self.state["why"] = "nothing to catch up"
            return 0
        paused = self.pause()
        if paused:                                       # fire work first: nothing goes, to whisper or to AWS
            self.state["why"] = f"paused: {paused}"
            return 0
        whisper = [r for r in rows if r.get("route") == "whisper"]      # (in order: the alert tier, then the newest)
        aws: dict[str, list[dict]] = {}
        for r in rows:
            if r.get("route") == "aws":
                aws.setdefault(str(r["key"]), []).append(r)
        sent, why = 0, []
        for key, rs in aws.items():
            if not self.aws_idle(key):
                why.append(f"{key}: AWS busy")
                continue
            for r in rs[:AWS_BATCH]:
                ok, never = self.to_aws(r)
                if ok:
                    self._note_sent(str(r["id"]), now)
                    sent += 1
                elif never:
                    self.give_up(str(r["id"]), never)
        if whisper:
            ok, busy = self.whisper_ready(self._whisper_at)
            if ok:
                got = self.to_whisper(whisper[:WHISPER_BATCH])
                if got is None:                          # the call failed: nothing was tried, nothing is counted
                    why.append("whisper: the station didn't take the call")
                else:
                    for cid in got.get("queued") or ():
                        self._note_sent(str(cid), now)
                        sent += 1
                    for cid, never in (got.get("refused") or {}).items():
                        self.give_up(str(cid), str(never))
                    if got.get("queued"):
                        self._whisper_at = now
                    if got.get("hold"):
                        why.append(f"whisper: {got['hold']}")
            else:
                why.append(f"whisper: {busy}")
        self.state["sent"] = self.state.get("sent", 0) + sent
        self.state["why"] = "; ".join(why)
        return sent

    def _note_sent(self, cid: str, now: float) -> None:
        n, _at = self._sent.get(cid, (0, 0.0))
        self._sent[cid] = (n + 1, now)

    def stats(self) -> dict:
        """Settings > Health: on, the backlog waiting, given up, caught up in the last 24 h, why it waits (if it does)."""
        return {k: self.state.get(k) for k in ("on", "max_age_h", "waiting", "gave_up", "caught_24h", "last_at", "why",
                                               "sent", "error")}
