"""The station's signal metrics (station side, #605): what it measures on the air, as numbers, to the hub, so the
squelch and the gain of each channel can be tuned from how they look over days (after an antenna or splitter change,
say). The hub keeps them (sizeup/cloud/metricstore.py) and serves them to admins (GET /api/cad/metrics). The station's
own log lines stay exactly as they are: this only adds numbers beside them.

Records (one small JSON object each; "k" its kind, "s" its subject, "at" its time):

    floor   every running FM / AM channel, each minute ("s": the channel's label; "at": the minute's start). From one
            look a second at its squelch while closed: idle quieting q50 / q90 / qmax (FM: dB under the idle hiss; AM:
            the carrier level over the floor, dB), the hiss (FM: "hiss", relative dB) or the floor (AM: "floor", dBFS),
            seconds open ("open_s"), opens that kept nothing ("noise": radio/fm.py's short opens), looks ("n"); and
            its settings (SETTINGS: the squelch as running "open" / "close" / "min", the dongle "sdr", its "gain",
            "mhz", "kind", "state", "am") when they changed and every FULL_EVERY minutes (the hub carries them forward
            when read: half the bytes)
    tx      each transmission as the squelch closed it (App._segment): kind voice | page | burst (only a radio-ID burst)
            | noise (kept, no speech, no ID), its start, length ("dur"), quieting / carrier over the floor ("q"), at
            open and close ("qo", "qc"), the hiss it was measured against ("hiss"; AM: "floor" dBFS, "cf" the carrier
            dBFS, "snr" the voice's SNR), a background carrier ("bg"), why it ended ("why"), an ID decoded ("id")
    cc      each P25 system's control channel, each minute ("s": "p25" the county's, "p25:<system>" another's): the
            decode rate as trunk-recorder logs it every 3 s (avg "rate", "lo", "hi", lines "n"), retunes, voice grants
            on a frequency no dongle covers ("unc": {MHz: count}), state, the dongle(s)
    ev      what explains a shift on a graph: start (this station started), sdr (the dongle a channel runs on changed),
            squelch, gain, unplug, back, stall, down, up, off, on, cc_lost, cc_back, dropped (records lost while the hub
            was away). The hub adds the Settings audit's squelch and channel changes itself.

How it never gets in the way:
    App._segment -> Collector.tx(): a dict appended to a bounded deque under a lock, nothing else (no I/O, no numpy)
    the sampler thread ("station-metrics") reads attributes the receivers already keep, once a second; never a dongle
    the sender thread ("hub-metrics") posts batches to POST /api/station/metrics (gzipped, every SEND_S, at most
    BATCH_MAX records) on its own connection: never the outbox (pages and transmissions don't queue behind metrics)
    while the hub is away records wait in memory, at most MAX_RECORDS: past that the oldest are dropped and counted
    (Collector.dropped), and an "ev dropped" says so once the hub is back. A restart loses what waited (never on disk).
    a new hub task starts from the last copy of metrics.sqlite (sizeup/cloud/metricstore.py: not in Litestream), up to
    3 h old; its first answer to this station says "resend_from" (when that copy was made), and what the hub had acked
    since then (kept RESEND_KEEP_S) goes up again: a deploy loses nothing while the station is connected
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

from sizeup.cloud.client import Backoff, HubError

PATH = "/api/station/metrics"
TICK_S = 1.0               # the sampler's look at each squelch
DIFF_S = 5.0               # ... and at what changed (state, dongle, squelch, gain, the control channel)
SEND_S = 60.0              # a batch every minute (sooner while a backlog goes up)
BATCH_MAX = 2000           # records a post (~250 KB as JSON, ~30 KB gzipped)
MAX_RECORDS = 20000        # waiting while the hub is away: about a day and a half
RESEND_KEEP_S = 6 * 3600.0     # what the hub has acked is kept this long more, for a hub that lost it (a deploy) ...
RESEND_MAX = 12000             # ... at most this many
RESEND_MARGIN_S = 300.0        # ... and asked for from this much earlier than the hub says (the two clocks)
REJECT = (400, 413, 415, 422)   # the hub will never take this batch: dropped (counted), not retried
SETTINGS = ("kind", "am", "mhz", "open", "close", "min", "sdr", "gain", "state")   # a floor record's slow-changing part
FULL_EVERY = 15            # ... sent when it changed, and on every 15th minute regardless

_r1 = lambda v: None if v is None else round(float(v), 1)   # noqa: E731


def _pct(vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p 0-100) of vals, None when empty."""
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round(p / 100.0 * len(s) + 0.5)) - 1))
    return s[k]


class Collector:
    """The records waiting for the hub, oldest first, each with its seq (ack(seq) drops what the hub has)."""

    def __init__(self, max_records: int = MAX_RECORDS, clock: Callable[[], float] = time.time):
        self.max_records, self.clock = max_records, clock
        self._q: deque[tuple[int, dict]] = deque()
        self._seq = 0
        self._lock = threading.Lock()
        self.dropped = 0          # records dropped (the oldest first) because too many waited
        self._done: deque[tuple[float, dict]] = deque()   # (acked at, record): kept for resend()
        self.added = 0
        self.event("station", "start", "sizeup started")

    def add(self, rec: dict) -> None:
        with self._lock:
            self._seq += 1
            self._q.append((self._seq, rec))
            self.added += 1
            while len(self._q) > self.max_records:
                self._q.popleft()
                self.dropped += 1

    def batch(self, n: int = BATCH_MAX) -> tuple[int, list[dict]]:
        """(the last seq in it, the oldest n records); (0, []) when nothing waits."""
        with self._lock:
            got = [self._q[k] for k in range(min(n, len(self._q)))]
        return (got[-1][0], [r for _s, r in got]) if got else (0, [])

    def ack(self, upto: int, now: float | None = None) -> int:
        """The hub has everything up to seq `upto`: out of the queue, kept RESEND_KEEP_S more (resend())."""
        now = self.clock() if now is None else now
        n = 0
        with self._lock:
            while self._q and self._q[0][0] <= upto:
                self._done.append((now, self._q.popleft()[1]))
                n += 1
            while self._done and (len(self._done) > RESEND_MAX or self._done[0][0] < now - RESEND_KEEP_S):
                self._done.popleft()
        return n

    def resend(self, since: float) -> int:
        """What the hub acked from `since` on (this station's clock), queued again: the hub lost it (it started from
        an older copy). The number queued."""
        with self._lock:
            again = [r for t, r in self._done if t >= since]
            self._done = deque((t, r) for t, r in self._done if t < since)
        for r in again:
            self.add(r)
        return len(again)

    def pending(self) -> int:
        return len(self._q)

    def event(self, subject: str, ev: str, detail: str = "", at: float | None = None) -> None:
        self.add({"k": "ev", "s": str(subject)[:80], "at": round(self.clock() if at is None else at, 3),
                  "ev": ev, "d": str(detail)[:300]})

    def tx(self, label: str, kind: str, seg, ids: bool = False, am: bool = False) -> None:
        """One transmission as its squelch closed it (radio/fm.py / radio/am.py Segment): numbers only, never audio."""
        r: dict = {"k": "tx", "s": str(label)[:80], "at": round(float(seg.start), 3),
                   "dur": round(float(seg.end - seg.start), 2), "kind": kind, "q": _r1(seg.quieting_db)}
        for key, attr in (("qo", "q_open"), ("qc", "q_close"), ("floor" if am else "hiss", "hiss_db"),
                          ("bg", "floor_db"), ("cf", "carrier_dbfs"), ("snr", "snr_db")):
            v = getattr(seg, attr, None)
            if v is not None:
                r[key] = _r1(v)
        if getattr(seg, "reason", ""):
            r["why"] = str(seg.reason)[:20]
        if ids:
            r["id"] = 1
        if getattr(seg, "weak", False):
            r["weak"] = 1
        if getattr(seg, "cont", False):
            r["cont"] = 1
        self.add(r)


def _serial(device) -> str:
    """The dongle a receiver runs on: its serial (a lookup made by the app), the configured string, or ""."""
    s = getattr(device, "serial", None)
    if s:
        return str(s)
    return device if isinstance(device, str) else ""


class Sampler:
    """Once a second, each running channel's squelch (idle quieting, open or not, the hiss / floor); each minute, a
    floor record per channel and a cc record per P25 system; every DIFF_S, the events (what changed)."""

    def __init__(self, app, collector: Collector, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print):
        self.app, self.c, self.clock, self.log = app, collector, clock, log
        self._errors: set[tuple[str, str]] = set()
        self._minute: int | None = None
        self._acc: dict[str, dict] = {}            # label -> this minute's looks
        self._prev: dict[str, dict] = {}           # subject -> what it was at the last diff
        self._sent: dict[str, tuple] = {}          # label -> its SETTINGS as last sent in a floor record
        self._diff_at = 0.0

    # -- what runs ---------------------------------------------------------------------------------------------------

    def _assigned(self) -> set | None:
        """The jobs a dongle has (switchable dongles), None when each channel has its own."""
        app = self.app
        return set(app.assign.values()) if getattr(app, "shared", False) else None

    def channels(self):
        """(label, kind, am, receiver, squelch) for each frequency a receiver demodulates."""
        app = self.app
        kinds = getattr(app, "_kind", {})
        for r in list(getattr(app, "receivers", []) or []):
            am = bool(getattr(r, "subs", None) is not None)
            for s in (getattr(r, "subs", None) or [None]):
                ch = s.channel if s is not None else r.channel
                sq = s.squelch if s is not None else r.squelch
                yield ch.label, kinds.get(ch.label, ""), am, r, sq, ch

    def _running(self, r, label: str, assigned: set | None) -> str:
        """The receiver's state as the station's own rows say it ("off" when no dongle has its job)."""
        if assigned is not None and r.channel.label not in assigned:
            return "off"
        return str(getattr(r, "state", "") or "")

    def recorders(self):
        """(subject, recorder, state) of each P25 system: "p25" the county's, "p25:<name>" another's."""
        app = self.app
        assigned = self._assigned()
        rec = getattr(app, "recorder", None)
        if rec is not None and getattr(app, "p25", None) is not None:
            from sizeup.app import P25_MODE
            yield "p25", rec, ("off" if assigned is not None and P25_MODE not in assigned else None)
        for name, rt in list((getattr(app, "systems", None) or {}).items()):
            on = rt.running(getattr(app, "assign", {}).values()) if hasattr(rt, "running") else [1]
            yield f"p25:{name}", rt.recorder, (None if on else "off")

    # -- each second ------------------------------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        minute = int(now // 60)
        if self._minute is not None and minute != self._minute:
            done, self._minute = self._minute, minute
            self.flush(done)                       # (the minute moves on whatever the flush does)
        self._minute = minute
        assigned = self._assigned()
        for label, kind, am, r, sq, ch in self.channels():
            if self._running(r, label, assigned) != "ok":
                continue
            a = self._acc.setdefault(label, {"q": [], "n": 0, "open": 0, "hiss": [], "kind": kind, "am": am, "r": r,
                                             "sq": sq, "mhz": ch.mhz})
            a.update(r=r, sq=sq, mhz=ch.mhz)
            a["n"] += 1
            try:
                if sq.open:
                    a["open"] += 1
                else:
                    q = sq.quieting_db
                    if q is not None:
                        a["q"].append(float(q))
                h = sq.hiss_db
                if h is not None:
                    a["hiss"].append(float(h))
            except Exception:  # noqa: BLE001 - a squelch being swapped (an rtl_fm restart): the next look
                pass
        if now - self._diff_at >= DIFF_S:
            self._diff_at = now
            self.diff(now)

    # -- each minute ------------------------------------------------------------------------------------------------

    def flush(self, minute: int) -> None:
        """The minute's records: each channel's and each P25 system's on its own, so one that can't be read (a receiver
        half swapped, a recorder gone) costs only its own record."""
        acc, self._acc = self._acc, {}
        for label, a in acc.items():
            try:
                self._floor(minute, label, a)
            except Exception as e:  # noqa: BLE001 - that channel's minute only
                self._said(f"floor {label}", e)
        for subject, rec_, off in self.recorders():
            try:
                self._cc(minute, subject, rec_, off)
            except Exception as e:  # noqa: BLE001 - that system's minute only
                self._said(f"cc {subject}", e)

    def _said(self, what: str, e: Exception) -> None:
        """A record that couldn't be made: logged once per (what, error type)."""
        key = (what, type(e).__name__)
        if key not in self._errors:
            self._errors.add(key)
            self.log(f"station metrics: {what} not recorded: {type(e).__name__}: {e}")

    def _floor(self, minute: int, label: str, a: dict) -> None:
        t0, t1 = minute * 60.0, minute * 60.0 + 60.0
        r, sq = a["r"], a["sq"]
        rec: dict = {"k": "floor", "s": label[:80], "at": t0, "kind": a["kind"], "n": a["n"],
                     "mhz": round(float(a["mhz"]), 4), "open_s": a["open"]}
        if a["am"]:
            rec["am"] = 1
        hiss = _pct(a["hiss"], 50)
        if hiss is not None:
            rec["floor" if a["am"] else "hiss"] = round(hiss, 1)
        for key, p in (("q50", 50), ("q90", 90), ("qmax", 100)):
            v = _pct(a["q"], p)
            if v is not None:
                rec[key] = round(v, 1)
        shorts = getattr(r, "shorts", None)
        if shorts is not None:
            rec["noise"] = sum(1 for t in list(shorts) if t0 <= t < t1)
        params = getattr(sq, "params", None)
        if params is not None:
            from sizeup.radio.fm import FRAME, RATE
            rec.update(open=params[0], close=params[1], min=round(params[2] * FRAME / RATE, 2))
        else:
            rec.update(open=getattr(sq, "open_db", None), close=getattr(sq, "close_db", None))
        rec["sdr"] = _serial(getattr(r, "device", None))
        if getattr(r, "gain_db", None) is not None:
            rec["gain"] = r.gain_db
        rec["state"] = str(getattr(r, "state", ""))
        settings = tuple(rec.get(k) for k in SETTINGS)
        if minute % FULL_EVERY and self._sent.get(label) == settings:
            for k in SETTINGS:
                rec.pop(k, None)
        else:
            self._sent[label] = settings
        self.c.add(rec)

    def _cc(self, minute: int, subject: str, rec_, off) -> None:
        t0 = minute * 60.0
        cc = getattr(rec_, "cc", None)
        if cc is None or not hasattr(cc, "take_minute"):
            return
        m = cc.take_minute()
        state = off or str(getattr(rec_, "state", "") or "")
        if state != "ok" and not m["n"] and not m["uncovered"]:
            return
        out: dict = {"k": "cc", "s": subject, "at": t0, "n": m["n"], "state": state,
                     "sdr": str(rec_.on() or "") if hasattr(rec_, "on") else ""}
        if m["n"]:
            out.update(rate=round(m["sum"] / m["n"], 1), lo=m["lo"], hi=m["hi"])
        if m["retunes"]:
            out["retunes"] = m["retunes"]
        if m["uncovered"]:
            out["unc"] = dict(m["uncovered"])
        self.c.add(out)

    # -- what changed -----------------------------------------------------------------------------------------------

    def _ev(self, subject: str, ev: str, detail: str, now: float) -> None:
        self.c.event(subject, ev, detail, now)

    def diff(self, now: float) -> None:
        """What changed since the last look: each channel and each P25 system on its own (one that can't be read is
        skipped, said once)."""
        assigned = self._assigned()
        seen: set[str] = set()
        for label, _kind, _am, r, sq, _ch in self.channels():
            if label in seen:
                continue
            seen.add(label)
            try:
                params = getattr(sq, "params", None)
                sqv = None
                if params is not None:
                    from sizeup.radio.fm import FRAME, RATE
                    sqv = (params[0], params[1], round(params[2] * FRAME / RATE, 2))
                cur = {"state": self._running(r, label, assigned), "detail": str(getattr(r, "detail", "") or ""),
                       "restarts": int(getattr(r, "restarts", 0) or 0), "sdr": _serial(getattr(r, "device", None)),
                       "gain": getattr(r, "gain_db", None), "squelch": sqv}
                self._changed(label, cur, now)
            except Exception as e:  # noqa: BLE001
                self._said(f"events {label}", e)
        for subject, rec, off in self.recorders():
            try:
                st = rec.status()
                cc = getattr(rec, "cc", None)
                ccs = cc.status() if cc is not None else {}
                lost = bool(ccs.get("low_since") or ccs.get("silent_since"))
                cur = {"state": off or str(st.get("state") or ""), "detail": str(st.get("detail") or ""),
                       "restarts": int(st.get("restarts") or 0), "sdr": str(rec.on() or "") if hasattr(rec, "on") else "",
                       "cc_lost": lost if (off or st.get("state")) == "ok" else None}
                self._changed(subject, cur, now)
            except Exception as e:  # noqa: BLE001
                self._said(f"events {subject}", e)

    def _changed(self, subject: str, cur: dict, now: float) -> None:
        prev = self._prev.get(subject)
        self._prev[subject] = cur
        if prev is None:
            return                                   # the first look: what is, not a change
        was, st = prev["state"], cur["state"]
        if st != was:
            if st == "absent":
                self._ev(subject, "unplug", cur["detail"], now)
            elif was == "absent" and st == "ok":
                self._ev(subject, "back", f"SDR {cur['sdr']}" if cur["sdr"] else "", now)
            elif st == "down":
                self._ev(subject, "stall" if "stalled" in cur["detail"] else "down", cur["detail"], now)
            elif st == "off":
                self._ev(subject, "off", "", now)
            elif was == "off":
                self._ev(subject, "on", f"SDR {cur['sdr']}" if cur["sdr"] else "", now)
            elif st == "ok":
                self._ev(subject, "up", "", now)
        elif cur["restarts"] > prev["restarts"] and st == "ok":
            # restarted between two looks (down and up again within DIFF_S): still a gap in its audio
            self._ev(subject, "stall" if "stalled" in prev["detail"] else "down",
                     f"restarted {cur['restarts'] - prev['restarts']}x", now)
        if cur["sdr"] != prev["sdr"] and (cur["sdr"] or prev["sdr"]):
            self._ev(subject, "sdr", f"SDR {prev['sdr'] or 'none'} -> {cur['sdr'] or 'none'}", now)
        if "squelch" in cur and cur["squelch"] != prev.get("squelch") and cur["squelch"] and prev.get("squelch"):
            o, c, m = cur["squelch"]
            po, pc, pm = prev["squelch"]
            self._ev(subject, "squelch", f"open {o:g} close {c:g} min {m:g} (was open {po:g} close {pc:g} min {pm:g})", now)
        if "gain" in cur and cur["gain"] != prev.get("gain"):
            self._ev(subject, "gain", f"{prev.get('gain')} -> {cur['gain']}", now)
        if cur.get("cc_lost") is not None and prev.get("cc_lost") is not None and cur["cc_lost"] != prev["cc_lost"]:
            self._ev(subject, "cc_lost" if cur["cc_lost"] else "cc_back", "", now)


class MetricsLink:
    """The sampler's thread and the sender's, beside the station's hub link (sizeup/cloud/station.py). The Collector
    is the app's (app.metrics), kept across the link going off and on."""

    def __init__(self, app, cfg, client: Callable[[float], object], log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time):
        self.app, self.cfg, self.log, self.clock = app, cfg, log, clock
        self.station = str(getattr(cfg, "station_id", "") or "")   # the id the hub assigns, once it has (StationLink.adopt)
        if getattr(app, "metrics", None) is None:
            app.metrics = Collector(clock=clock)
        self.c: Collector = app.metrics
        self.sampler = Sampler(app, self.c, clock, log)
        self._client = client
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.sent = 0
        self.rejected = 0
        self.last_ok: float | None = None
        self.error = ""
        self._dropped_said = 0
        self._backoff = Backoff(first=5.0, fast_cap=30.0, cap=300.0)
        self._next = 0.0

    def start(self) -> MetricsLink:
        if self._threads:
            return self
        for name, fn in (("station-metrics", self._sample), ("hub-metrics", self._send)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        self._stop.set()

    def _sample(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.sampler.tick()
            except Exception as e:  # noqa: BLE001 - the metrics never take anything down
                if self.error != f"sampler: {type(e).__name__}":
                    self.error = f"sampler: {type(e).__name__}"
                    self.log(f"station metrics: sampler: {type(e).__name__}: {e}")

    def _send(self) -> None:
        client = self._client(20.0)
        while not self._stop.is_set():
            try:
                wait = self.step(client)
            except Exception as e:  # noqa: BLE001 - logged, and the thread goes on (a minute later)
                wait = SEND_S
                if self.error != f"sender: {type(e).__name__}":
                    self.error = f"sender: {type(e).__name__}"
                    self.log(f"station metrics: sender: {type(e).__name__}: {e}")
            self._stop.wait(wait)

    def step(self, client, now: float | None = None) -> float:
        """One send if one is due: the seconds until the next."""
        now = self.clock() if now is None else now
        if now < self._next:
            return min(SEND_S, self._next - now)
        upto, recs = self.c.batch(BATCH_MAX)
        if not recs:
            self._next = now + SEND_S
            return SEND_S
        body = {"v": 1, "station": self.station or self.cfg.station_id, "records": recs}
        try:
            answer = client.request("POST", PATH, body, timeout=20.0, gzip_body=True)
        except HubError as e:
            if e.status in REJECT:
                self.c.ack(upto)                       # never taken: not sent again and again
                self.rejected += len(recs)
                self.log(f"station metrics: the hub refused a batch of {len(recs)} ({e}): dropped")
                return 1.0
            wait = self._backoff.fail(e)
            if not self.error.startswith("hub"):
                self.log(f"station metrics: not sent ({e}): {self.c.pending()} waiting, at most {self.c.max_records}")
            self.error = f"hub: {e}"[:200]
            self._next = now + wait
            return wait
        self._backoff.ok()
        self.c.ack(upto, now)
        since = answer.get("resend_from") if isinstance(answer, dict) else None
        if isinstance(since, (int, float)) and not isinstance(since, bool):
            n = self.c.resend(float(since) - RESEND_MARGIN_S)
            if n:
                self.log(f"station metrics: the hub started from an older copy: {n} record(s) sent again")
        self.sent += len(recs)
        self.last_ok, self.error = now, ""
        if self.c.dropped > self._dropped_said:       # said once the hub is back: the gap on its graphs explained
            n = self.c.dropped - self._dropped_said
            self._dropped_said = self.c.dropped
            self.c.event("station", "dropped", f"{n} metric record(s) dropped while the hub was away", now)
        more = self.c.pending() > 0
        self._next = now + (1.0 if more else SEND_S)
        return 1.0 if more else SEND_S

    def stats(self) -> dict:
        return {"pending": self.c.pending(), "dropped": self.c.dropped, "sent": self.sent, "rejected": self.rejected,
                "last_ok": self.last_ok, "error": self.error}
