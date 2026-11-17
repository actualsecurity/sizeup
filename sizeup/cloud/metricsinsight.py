"""The station's signal metrics read back (#606, on top of #605's sizeup/cloud/metricstore.py): a squelch suggested per
channel from weeks of its own noise and traffic, the shifts after an event (a rewire, a dongle moved, a restart) as
Health warnings, and the compact 24 h lines Health draws. The hub's; admins only (GET /api/cad/metrics?view=...).

Suggested squelch (suggest(); FM traffic channels can apply it, paging and AM get it for information only):

    noise      the p90, over the last SUGGEST_DAYS, of each idle minute's q90 (the squelch closed the whole minute,
               open_s == 0): how far the hiss alone quiets itself on nine minutes in ten. Needs MIN_FLOOR_MIN minutes
               (24 h) of floor records, else "not enough data"
    open       ceil(noise + margin_db) (Settings, default 3 dB), clamped to the Squelch card's range
    check      against the real transmissions kept over the same days (voice, a radio-ID burst, a page: never the
               "noise" kind), each at its quieting at open ("qo", else "q"): if more than miss_pct (default 2%) of them
               are under that open, it is capped at that percentile of theirs minus WEAK_GAP_DB and flagged "would miss
               weak traffic". So it never sits above the weak traffic the channel really carries
    raise      only with at least MIN_REAL_TX real transmissions to check it against; with fewer, never above the
               Open running now ("too few to check a raise")
    chatter    the idle minutes only exist below the Open (an excursion over it opens the squelch, and that minute
               isn't idle): a channel chattering (its noise opens' median at CHATTER_PER_MIN or more) reads low, so it
               is never suggested lower than it runs, and at least one dB higher (then look again)
    close      open minus the running hysteresis (Open - Close), at least the range's lowest, under the open
    paging     the same numbers, information only: its squelch is read-only (a high Open would mute the pages); never
               above the running Open, and capped PAGING_GAP_DB (not WEAK_GAP_DB) under its weak pages

Shift warnings (shifts(); the hub's janitor runs it every 10 min, Health shows the result as "metrics_shift" hints,
never a push; mutable like the alerts, Settings > System > Push notifications):

    after an event (EVENTS: the station's sdr / gain / squelch / unplug / back / on / start, or the Settings audit's
    squelch change to the channel) in the last WARN_S, the AFTER_S after it (from its first SETTLE_S, at least
    MIN_AFTER_S of data, never past the next event) against the BEFORE_S before it (never before the event before);
    with later events, what it found must still show from the newest one on (a dongle moved back: no warning; a squelch
    change since: the idle checks can't tell any more):
        FM       the idle minutes' q90 median up more than shift_db; the noise opens a minute up opens_x times and at
                 least OPENS_MIN (neither after a squelch change: the Open itself moves both)
        AM       the floor (dBFS) median up more than shift_db
        traffic  the transmissions an hour (the median of half-hour counts: a fire's hour of traffic moves it
                 little) against the same clock hours the day before (a quiet night after a busy evening is no shift):
                 rate_x times fewer, from at least RATE_MIN; or the kept noise key-ups rate_x times more, to at least
                 RATE_MIN (what a gain, a squelch or interference brings)
        paging   the pages' quieting at open, median down more than shift_db over at least PAGES_MIN pages after
                 (windows PAGE_BEFORE_S / PAGE_AFTER_S: pages are few)
        P25      the control channel's decode rate median down more than cc_drop_pct %
    with no event, a drift check once a day: the last DRIFT_S's hourly medians against the DRIFT_BEFORE_S before, the
    same thresholds (FM idle q90, AM floor, P25 decode rate). A step a day old moves neither median: no warning
    without an event, until it has lasted most of a week.

Bounded: each check reads one series' raw records for at most a day and a quarter (pages: ten days), at most
MAX_EVENTS events a subject; the suggestions are kept CACHE_S. Nothing here runs on the alert path.
"""

from __future__ import annotations

import math
import threading
from urllib.parse import quote
import time
from typing import Callable

from sizeup import tz

SECTION = "signal_metrics"     # the CAD settings section (sizeup/cad/settings.py), audited as Radios
DEFAULTS = {"margin_db": 3.0, "miss_pct": 2.0, "shift_db": 3.0, "opens_x": 3.0, "rate_x": 3.0, "cc_drop_pct": 30.0}
LIMITS = {"margin_db": (1.0, 10.0), "miss_pct": (0.0, 10.0), "shift_db": (1.0, 15.0), "opens_x": (1.5, 20.0),
          "rate_x": (1.5, 20.0), "cc_drop_pct": (5.0, 90.0)}
LABELS = {"margin_db": "Suggested Open: margin over the noise p90 (dB)",
          "miss_pct": "Suggested Open: real transmissions it may miss (%)",
          "shift_db": "Shift warning: idle quieting / floor / page level (dB)",
          "opens_x": "Shift warning: noise opens a minute (×)",
          "rate_x": "Shift warning: transmissions an hour (×)",
          "cc_drop_pct": "Shift warning: P25 decode rate down (%)"}

SUGGEST_DAYS = 7
MIN_FLOOR_MIN = 1440           # 24 h of floor records, else "not enough data"
MIN_IDLE_MIN = 60              # ... of which at least this many idle
MIN_REAL_TX = 20               # real transmissions to check a raise against
WEAK_GAP_DB = 1.0              # capped this far under the weak traffic's percentile
PAGING_GAP_DB = 3.0            # ... a paging channel's (information only, conservative)
CHATTER_PER_MIN = 2.0          # noise opens a minute: chattering (sizeup/channels.py CHATTER_PER_MIN)
REAL_KINDS = ("voice", "burst", "page")
STEP = 0.5                     # the Squelch card's step (sizeup/channels.py SQUELCH)
OPEN_RANGE, CLOSE_RANGE = (4.0, 30.0), (2.0, 28.0)
CACHE_S = 600.0
HIST_LO, HIST_HI = -10.0, 50.0  # the distributions' bins (dB, STEP wide)

EVENTS = {"sdr": "SDR change", "gain": "gain change", "squelch": "squelch change", "unplug": "unplugged",
          "back": "plugged back in", "on": "dongle assigned", "start": "station restart", "setting": "settings change"}
WARN_S = 48 * 3600.0           # an event this recent is checked (and its warning shown)
BEFORE_S, AFTER_S = 24 * 3600.0, 6 * 3600.0
SETTLE_S = 300.0               # the event's first minutes left out
MIN_AFTER_S = 1800.0           # at least this much after it, to say anything
MERGE_S = 600.0                # events this close together: one
MAX_EVENTS = 6
OPENS_MIN = 0.5
RATE_MIN = 4.0                 # transmissions an hour (the medians') to say a rate moved
RATE_BIN_S = 1800.0
PAGES_MIN = 3
PAGE_BEFORE_S, PAGE_AFTER_S = 7 * 86400.0, 72 * 3600.0
DRIFT_S, DRIFT_BEFORE_S = 7 * 86400.0, 21 * 86400.0
DRIFT_EVERY_S = 86400.0
SPARK_S, SPARK_BIN_S = 24 * 3600.0, 1800.0
MAX_DOCS = 20000


def settings(section: dict | None) -> dict:
    """The thresholds in force: DEFAULTS over what Settings has (what can't be read: the default)."""
    out = dict(DEFAULTS)
    for k, v in (section or {}).items():
        if k in LIMITS and isinstance(v, (int, float)) and not isinstance(v, bool) and LIMITS[k][0] <= v <= LIMITS[k][1]:
            out[k] = float(v)
    return out


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def pct(vals: list[float], p: float) -> float | None:
    """Nearest-rank percentile (p 0-100), None when empty (sizeup/cloud/metricstore.py _pct)."""
    if not vals:
        return None
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, int(math.ceil(p / 100.0 * len(s))) - 1))]


def median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _r(v: float | None, d: int = 1) -> float | None:
    return None if v is None else round(v, d)


def _hist(vals: list[float]) -> dict:
    """Counts per STEP-wide bin from HIST_LO (values outside go to the end bins): {lo, step, n: [..]}."""
    bins = [0] * int((HIST_HI - HIST_LO) / STEP)
    for v in vals:
        k = int((min(max(v, HIST_LO), HIST_HI - 1e-9) - HIST_LO) // STEP)
        bins[k] += 1
    while bins and not bins[-1]:
        bins.pop()
    lead = next((i for i, b in enumerate(bins) if b), len(bins))
    return {"lo": HIST_LO + lead * STEP, "step": STEP, "n": bins[lead:]}


def _settings_now(floor: list[dict]) -> dict:
    """The newest floor record's settings (they are sent when they change and every 15 min)."""
    for d in reversed(floor):
        if "open" in d or "kind" in d:
            return d
    return {}


def _step_up(v: float) -> float:
    return math.ceil(v / STEP - 1e-9) * STEP


def _step_down(v: float) -> float:
    return math.floor(v / STEP + 1e-9) * STEP


# -- the suggested squelch --------------------------------------------------------------------------------------------

def suggest(floor: list[dict], tx: list[dict], cfg: dict, now: float, span_s: float = SUGGEST_DAYS * 86400.0) -> dict:
    """A channel's suggested squelch from its floor and tx records (oldest first) over span_s. {state: "ok" | "short",
    open, close, apply (an admin may apply it: a traffic FM channel, and it differs), info (why not: paging, AM), the
    numbers it used (noise_p90, noise_max_p90, idle_min, minutes, real, under, weak, cur_open, cur_close, capped, ...),
    why (one line: the numbers), hist {idle, real, noise} (STEP-wide bins of q90 / qo / q)}."""
    cfg = settings(cfg)
    st = _settings_now(floor)
    kind = str(st.get("kind") or "")
    am = bool(st.get("am"))
    cur_o, cur_c = _num(st.get("open")), _num(st.get("close"))
    idle = [d for d in floor if _num(d.get("q90")) is not None and not d.get("open_s")]
    q90 = [float(d["q90"]) for d in idle]
    qmax = [float(d["qmax"]) for d in idle if _num(d.get("qmax")) is not None]
    real = [d for d in tx if d.get("kind") in REAL_KINDS]
    rq = [v for d in real if (v := _num(d.get("qo") if d.get("qo") is not None else d.get("q"))) is not None]
    nq = [v for d in tx if d.get("kind") == "noise" and (v := _num(d.get("q"))) is not None]
    opens = [v for d in floor if (v := _num(d.get("noise"))) is not None]
    out: dict = {"subject": "", "kind": kind, "am": am, "minutes": len(floor), "idle_min": len(idle), "real": len(rq),
                 "noise_tx": len(nq), "cur_open": cur_o, "cur_close": cur_c, "margin_db": cfg["margin_db"],
                 "miss_pct": cfg["miss_pct"], "days": round(span_s / 86400.0, 1),
                 "hist": {"idle": _hist(q90), "real": _hist(rq), "noise": _hist(nq)}}
    if len(floor) < MIN_FLOOR_MIN or len(idle) < MIN_IDLE_MIN:
        hrs = len(floor) / 60.0
        out.update(state="short", apply=False, why=f"not enough data: {hrs:.0f} h of {MIN_FLOOR_MIN / 60:.0f} h"
                   + (f", {len(idle)} idle min" if len(floor) >= MIN_FLOOR_MIN else ""))
        return out
    noise = pct(q90, 90)
    out["noise_p90"] = _r(noise)
    out["noise_max_p90"] = _r(pct(qmax, 90))
    paging = kind == "paging"
    want = float(math.ceil(noise + cfg["margin_db"] - 1e-9))
    notes = []
    chatter = median(opens) or 0.0
    out["opens_p50"] = _r(chatter, 2)
    if chatter >= CHATTER_PER_MIN and cur_o is not None and want < cur_o + 1:
        want = cur_o + 1.0
        notes.append(f"chattering {chatter:g}/min: idle reads low; +1 dB, look again")
    gap = PAGING_GAP_DB if paging else WEAK_GAP_DB
    capped = False
    if rq:
        weak = pct(rq, cfg["miss_pct"]) if cfg["miss_pct"] > 0 else min(rq)
        out["weak"] = _r(weak)
        under = sum(1 for v in rq if v < want)
        if under > len(rq) * cfg["miss_pct"] / 100.0:
            want, capped = _step_down(weak - gap), True
            notes.append(f"would miss weak traffic: {under} of {len(rq)} under")
    lo, hi = OPEN_RANGE
    want = min(max(want, lo), hi)
    if cur_o is not None and want > cur_o and (paging or len(rq) < MIN_REAL_TX):
        notes.append("paging: never above the running Open" if paging else f"{len(rq)} real tx: too few to check a raise")
        want = cur_o
    out["under"] = sum(1 for v in rq if v < want)
    hyst = (cur_o - cur_c) if cur_o is not None and cur_c is not None and cur_o > cur_c else 4.0
    close = min(CLOSE_RANGE[1], max(CLOSE_RANGE[0], _step_down(want - hyst)))
    if close >= want:
        close = want - STEP
    out.update(state="ok", open=_r(want), close=_r(close), capped=capped,
               same=cur_o is not None and abs(want - cur_o) < STEP / 2 and (cur_c is None or abs(close - cur_c) < STEP / 2))
    info = "paging: read-only" if paging else "AM: no squelch setting here" if am else \
        "" if kind in ("traffic", "") else f"{kind}: read-only"
    out["info"] = info
    out["apply"] = not info and not out["same"]
    out["why"] = (f"noise p90 {noise:.1f} dB ({len(idle):,} idle min, {out['days']:g} d) + {cfg['margin_db']:g} "
                  f"→ {math.ceil(noise + cfg['margin_db'] - 1e-9):g} · {out['under']}/{len(rq)} real under {want:g}"
                  + (f" · weak p{cfg['miss_pct']:g} {out['weak']:.1f}" if "weak" in out else "")
                  + "".join(f" · {n}" for n in notes))
    return out


# -- the shifts -------------------------------------------------------------------------------------------------------

def _event_name(ev: dict) -> str:
    return EVENTS.get(ev.get("ev"), str(ev.get("ev") or "event"))


def _is_squelch(ev: dict) -> bool:
    return ev.get("ev") == "squelch" or (ev.get("ev") == "setting" and "squelch/" in str(ev.get("detail") or ""))


def clusters(events: list[dict]) -> list[dict]:
    """The events that can explain a shift (EVENTS; of the Settings audit's, a squelch change), oldest first, those within MERGE_S of the first of a run merged
    into it: {at, ev, names, squelch (one of them changed the squelch), detail}."""
    out: list[dict] = []
    for e in sorted((e for e in events if e.get("ev") in EVENTS and isinstance(e.get("at"), (int, float))
                     and (e["ev"] != "setting" or _is_squelch(e))), key=lambda e: e["at"]):   # (record / transcribe: no)
        if out and e["at"] - out[-1]["at"] <= MERGE_S:
            c = out[-1]
            if _event_name(e) not in c["names"]:
                c["names"].append(_event_name(e))
            c["squelch"] = c["squelch"] or _is_squelch(e)
            continue
        out.append({"at": float(e["at"]), "ev": e["ev"], "names": [_event_name(e)], "squelch": _is_squelch(e),
                    "detail": str(e.get("detail") or "")[:120], "by": e.get("by") or ""})
    return out


def _vals(docs: list[dict], key: str, idle: bool = False) -> list[float]:
    return [v for d in docs if (not idle or not d.get("open_s")) and (v := _num(d.get(key))) is not None]


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _rates(tx: list[dict], t0: float, t1: float) -> float:
    """Transmissions an hour in [t0, t1): the median of its RATE_BIN_S counts (a burst of traffic moves it little)."""
    n = max(1, int((t1 - t0) // RATE_BIN_S))
    bins = [0] * n
    for d in tx:
        if t0 <= d["at"] < t1:
            bins[min(n - 1, int((d["at"] - t0) // RATE_BIN_S))] += 1
    return round((median([float(x) for x in bins]) or 0.0) * 3600.0 / RATE_BIN_S, 1)


def check_channel(before: list[dict], after: list[dict], btx: list[dict], atx: list[dict], bw: tuple[float, float],
                  aw: tuple[float, float], meta: dict, cfg: dict, squelch_changed: bool) -> list[dict]:
    """One channel's shift around one event: [{metric, text, before, after}] (empty: none). before / after: its floor
    records; btx / atx: its transmissions (btx None: no rate check); bw / aw: their windows (t0, t1): the same clock
    hours, the day before and after the event."""
    out = []
    am = bool(meta.get("am"))
    if am:
        b, a = median(_vals(before, "floor")), median(_vals(after, "floor"))
        if b is not None and a is not None and len(_vals(after, "floor")) >= MIN_AFTER_S / 60 * 0.5 and a - b > cfg["shift_db"]:
            out.append({"metric": "floor", "before": _r(b), "after": _r(a), "text": f"noise floor +{a - b:.1f} dB"})
    elif not squelch_changed:
        bi, ai = _vals(before, "q90", idle=True), _vals(after, "q90", idle=True)
        b, a = median(bi), median(ai)
        if len(bi) >= 60 and len(ai) >= 30 and a - b > cfg["shift_db"]:
            out.append({"metric": "idle_q90", "before": _r(b), "after": _r(a), "text": f"idle quieting +{a - b:.1f} dB"})
        bo, ao = _mean(_vals(before, "noise")), _mean(_vals(after, "noise"))
        if bo is not None and ao is not None and len(_vals(after, "noise")) >= 30 and ao >= OPENS_MIN \
                and ao >= cfg["opens_x"] * bo:
            out.append({"metric": "noise_opens", "before": _r(bo, 2), "after": _r(ao, 2),
                        "text": f"noise opens {ao:.1f}/min (was {bo:.1f})"})
    if btx is not None and meta.get("kind") != "paging" and len(after) >= MIN_AFTER_S / 60:   # (it ran)
        # medians of half-hour counts, so a fire's hour of traffic before or after never reads as a shift; up: the
        # kept noise key-ups only (what a gain, a squelch or interference brings); down: every kept transmission
        b_all, a_all = _rates(btx, *bw), _rates(atx, *aw)
        b_n = _rates([d for d in btx if d.get("kind") == "noise"], *bw)
        a_n = _rates([d for d in atx if d.get("kind") == "noise"], *aw)
        if b_all >= RATE_MIN and a_all * cfg["rate_x"] <= b_all:
            out.append({"metric": "tx_rate", "before": _r(b_all), "after": _r(a_all),
                        "text": f"transmissions {a_all:g}/h (was {b_all:g}/h)"})
        elif a_n >= RATE_MIN and a_n >= cfg["rate_x"] * b_n:
            out.append({"metric": "noise_tx", "before": _r(b_n), "after": _r(a_n),
                        "text": f"noise key-ups {a_n:g}/h (was {b_n:g}/h)"})
    return out


def check_pages(btx: list[dict], atx: list[dict], cfg: dict) -> list[dict]:
    """A paging channel's pages: their quieting at open, before against after."""
    bq = [v for d in btx if d.get("kind") == "page" and (v := _num(d.get("qo"))) is not None]
    aq = [v for d in atx if d.get("kind") == "page" and (v := _num(d.get("qo"))) is not None]
    if len(bq) >= PAGES_MIN and len(aq) >= PAGES_MIN:
        b, a = median(bq), median(aq)
        if b - a > cfg["shift_db"]:
            return [{"metric": "tx_q_open", "before": _r(b), "after": _r(a),
                     "text": f"pages open at {a:.1f} dB (was {b:.1f}, {len(aq)} pages)"}]
    return []


def check_cc(before: list[dict], after: list[dict], cfg: dict) -> list[dict]:
    """A P25 system's control channel: its decode rate (minutes it decoded), before against after."""
    b, a = median(_vals(before, "rate")), median(_vals(after, "rate"))
    if b and a is not None and len(_vals(after, "rate")) >= 30 and (b - a) / b * 100.0 > cfg["cc_drop_pct"]:
        return [{"metric": "cc_rate", "before": _r(b), "after": _r(a), "text": f"decode {a:.0f}/s (was {b:.0f}/s)"}]
    return []


class Insight:
    """The hub's: suggestions (kept CACHE_S), the shifts (from the janitor's turn), the 24 h sparks. store: the
    MetricStore; section(): the settings section (thresholds)."""

    def __init__(self, store, section: Callable[[], dict | None] = lambda: None, clock: Callable[[], float] = time.time,
                 log: Callable[[str], None] = print):
        self.store, self.section, self.clock, self.log = store, section, clock, log
        self._lock = threading.Lock()
        self._sugg: dict[tuple[str, str], tuple[float, dict]] = {}
        self._spark: tuple[float, dict] | None = None
        self.found: list[dict] = []           # the shifts in force (the last turn's)
        self._drift: list[dict] = []
        self._drift_at = 0.0
        self.turned_at: float | None = None

    def cfg(self) -> dict:
        try:
            return settings(self.section())
        except Exception:  # noqa: BLE001 - the defaults, never in the way
            return dict(DEFAULTS)

    def _series(self) -> dict[tuple[str, str], set[str]]:
        out: dict[tuple[str, str], set[str]] = {}
        for s in self.store.index()["series"]:
            out.setdefault((s["station"], s["subject"]), set()).add(s["kind"])
        return out

    def _meta(self, station: str, subject: str, now: float) -> dict:
        """The channel's kind / am from its newest floor record with settings (sent at least every 15 min; looked
        for over the last day: a channel off for a while still has them)."""
        return _settings_now(self.store.docs(station, subject, "floor", now - 86400.0, now + 60, 1500))

    # -- suggestions ------------------------------------------------------------------------------------------------

    def suggestion(self, station: str, subject: str, now: float | None = None) -> dict:
        now = self.clock() if now is None else now
        key = (station, subject)
        with self._lock:
            hit = self._sugg.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        t0 = now - SUGGEST_DAYS * 86400.0                 # (read outside the lock: Health's sparks never wait on it)
        floor = self.store.docs(station, subject, "floor", t0, now + 60, MAX_DOCS, newest=True)
        tx = self.store.docs(station, subject, "tx", t0, now + 60, MAX_DOCS, newest=True)
        out = suggest(floor, tx, self.cfg(), now)
        out.update(subject=subject, station=station, at=now)
        with self._lock:
            self._sugg[key] = (now, out)
        return out

    def forget(self, subject: str) -> None:
        """A channel's squelch changed (Settings): its suggestion worked out again at the next look."""
        with self._lock:
            for k in [k for k in self._sugg if k[1] == subject]:
                del self._sugg[k]

    def squelch(self, now: float | None = None) -> dict:
        """view=squelch: each FM / AM channel's suggestion (Settings > Radios > Squelch and Signal)."""
        now = self.clock() if now is None else now
        rows = [self.suggestion(st, s, now) for (st, s), kinds in sorted(self._series().items()) if "floor" in kinds]
        return {"channels": rows, "settings": self.cfg(), "at": now}

    # -- the shifts -------------------------------------------------------------------------------------------------

    def turn(self, now: float | None = None) -> list[dict]:
        """The janitor's turn (every 10 min, the hub's): each subject's events of the last WARN_S checked, and the
        drift once a day. The shifts in force, also kept in .found (Health reads that)."""
        now = self.clock() if now is None else now
        cfg = self.cfg()
        found: list[dict] = []
        series = self._series()
        for (station, subject), kinds in sorted(series.items()):
            if subject == "station":
                continue
            try:
                found += self._subject(station, subject, kinds, cfg, now)
            except Exception as e:  # noqa: BLE001 - that subject only
                self.log(f"metrics: shift check {subject}: {type(e).__name__}: {e}")
        if now - self._drift_at >= DRIFT_EVERY_S:
            self._drift_at = now
            drift = []
            for (station, subject), kinds in sorted(series.items()):
                if subject == "station":
                    continue
                try:
                    drift += self._drifted(station, subject, kinds, cfg, now)
                except Exception as e:  # noqa: BLE001
                    self.log(f"metrics: drift check {subject}: {type(e).__name__}: {e}")
            self._drift = drift
        have = {(f["subject"], f["metric"]) for f in found}
        found += [d for d in self._drift if (d["subject"], d["metric"]) not in have and now - d["at"] < DRIFT_EVERY_S * 1.5]
        self.found, self.turned_at = found, now
        return found

    def _subject(self, station: str, subject: str, kinds: set[str], cfg: dict, now: float) -> list[dict]:
        evs = clusters(self.store.events(station, subject, now - WARN_S - BEFORE_S, now + 60))
        recent = [c for c in evs if c["at"] >= now - WARN_S][-MAX_EVENTS:]
        out: dict[str, dict] = {}
        meta = self._meta(station, subject, now) if "floor" in kinds else {}
        def run(b0: float, b1: float, a0: float, a1: float, p1: float, squelch: bool, prev: list[float]) -> list[dict]:
            hits = []
            if "cc" in kinds:
                hits += check_cc(self.store.docs(station, subject, "cc", b0, b1),
                                 self.store.docs(station, subject, "cc", a0, a1), cfg)
            if "floor" in kinds:
                fb = self.store.docs(station, subject, "floor", b0, b1)
                fa = self.store.docs(station, subject, "floor", a0, a1)
                # the transmissions: the same clock hours the day before (traffic follows the day: a quiet night after
                # a busy evening isn't a shift); not when another event falls in those hours
                y0, y1 = a0 - 86400.0, a1 - 86400.0
                same = not any(y0 - SETTLE_S <= p <= y1 for p in prev)
                tb = self.store.docs(station, subject, "tx", y0, y1) if same else None
                ta = self.store.docs(station, subject, "tx", a0, a1)
                hits += check_channel(fb, fa, tb, ta, (y0, y1), (a0, a1), meta, cfg, squelch)
                if meta.get("kind") == "paging":
                    hits += check_pages(self.store.docs(station, subject, "tx", b1 - PAGE_BEFORE_S, b1),
                                        self.store.docs(station, subject, "tx", a0, p1), cfg)
            return hits

        for c in reversed(recent):                  # the newest event first: it explains what is now
            # its windows never cross another event: the before from the one before it, the after to the one after
            prev = [p["at"] for p in evs if p["at"] < c["at"]]
            later = [p for p in evs if p["at"] > c["at"]]
            b0 = max(c["at"] - BEFORE_S, prev[-1] + SETTLE_S if prev else 0.0)
            a0, a1 = c["at"] + SETTLE_S, min(now, c["at"] + AFTER_S, later[0]["at"] if later else now)
            if a1 - a0 < MIN_AFTER_S or c["at"] - b0 < 3600:
                continue
            p1 = min(now, c["at"] + PAGE_AFTER_S, later[0]["at"] if later else now)      # (pages: a longer after)
            hits = run(b0, c["at"], a0, a1, p1, c["squelch"], prev)
            if hits and later:
                # still so now? from the newest event on (a dongle moved back: no); a squelch change since: the idle
                # can't tell any more, so only what it still shows stays
                s0 = later[-1]["at"] + SETTLE_S
                if now - s0 >= MIN_AFTER_S:
                    still = {h["metric"] for h in run(b0, c["at"], s0, now, now, c["squelch"] or any(p["squelch"] for p in later),
                                                      prev + [p["at"] for p in later])}
                    hits = [h for h in hits if h["metric"] in still]
            for h in hits:
                if h["metric"] in out:
                    continue                          # a newer event explains it already
                out[h["metric"]] = {**h, "subject": subject, "station": station, "at": c["at"], "event": c["ev"],
                                    "names": c["names"], "detail": c["detail"], "by": c["by"],
                                    "text": f"{h['text']} since {tz.hhmm(c['at'])} ({', '.join(c['names'])})"}
        return list(out.values())

    def _drifted(self, station: str, subject: str, kinds: set[str], cfg: dict, now: float) -> list[dict]:
        """The last DRIFT_S's hourly medians against the DRIFT_BEFORE_S before (no event needed)."""
        out = []
        t1, t0 = now - DRIFT_S, now - DRIFT_S - DRIFT_BEFORE_S

        def med(metric: str, a: float, b: float) -> tuple[float | None, int]:
            rows = self.store.hours(station, subject, metric, a, b)
            return median([r["p50"] for r in rows if r["p50"] is not None]), len(rows)
        meta = self._meta(station, subject, now) if "floor" in kinds else {}
        checks = []
        if "cc" in kinds:
            checks.append(("cc_rate", "decode", True))
        if "floor" in kinds:
            checks.append(("floor", "noise floor", False) if meta.get("am") else ("idle_q90", "idle quieting", False))
        for metric, word, rate in checks:
            b, nb = med(metric, t0, t1)
            a, na = med(metric, t1, now)
            if b is None or a is None or nb < 72 or na < 72:
                continue                              # 3 days each at least
            if rate and b and (b - a) / b * 100.0 > cfg["cc_drop_pct"]:
                text = f"decode {a:.0f}/s over 7 d (was {b:.0f}/s)"
            elif not rate and a - b > cfg["shift_db"]:
                text = f"{word} +{a - b:.1f} dB over 7 d (no event)"
            else:
                continue
            out.append({"metric": metric, "subject": subject, "station": station, "at": now, "event": "drift",
                        "names": ["drift"], "detail": "", "by": "", "before": _r(b), "after": _r(a), "text": text})
        return out

    def hints(self) -> list[dict]:
        """Health's hints (kind "metrics_shift"): one a subject and metric, the newest first."""
        return [{"channel": f["subject"], "level": "warn", "kind": "metrics_shift", "text": f["text"], "at": f["at"],
                 "link": f"#settings/radios/signal?s={quote(f['subject'], safe='')}&at={int(f['at'])}"}
                for f in sorted(self.found, key=lambda f: -f["at"])]

    # -- Health's sparks --------------------------------------------------------------------------------------------

    def spark(self, now: float | None = None) -> dict:
        """view=spark: each subject's last SPARK_S in SPARK_BIN_S bins: FM / AM the idle q90 median (AM: the floor)
        and the noise opens a minute; P25 the decode rate. Kept 5 min."""
        now = self.clock() if now is None else now
        with self._lock:
            hit = self._spark
        if hit and now - hit[0] < 300:
            return hit[1]
        t0 = now - SPARK_S
        n = int(SPARK_S // SPARK_BIN_S)
        rows = []
        for (station, subject), kinds in sorted(self._series().items()):
            if subject == "station":
                continue
            row: dict = {"subject": subject, "station": station}
            if "floor" in kinds:
                docs = self.store.docs(station, subject, "floor", t0, now + 60, 3000)
                am = bool(_settings_now(docs).get("am"))
                q, o = [[] for _ in range(n)], [[] for _ in range(n)]
                for d in docs:
                    k = min(n - 1, max(0, int((d["at"] - t0) // SPARK_BIN_S)))
                    v = _num(d.get("floor" if am else "q90"))
                    if v is not None and (am or not d.get("open_s")):
                        q[k].append(v)
                    if (w := _num(d.get("noise"))) is not None:
                        o[k].append(w)
                row.update(am=am, level=[_r(median(x)) for x in q], opens=[_r(_mean(x), 2) for x in o])
            if "cc" in kinds:
                docs = self.store.docs(station, subject, "cc", t0, now + 60, 3000)
                r = [[] for _ in range(n)]
                for d in docs:
                    if (v := _num(d.get("rate"))) is not None:
                        r[min(n - 1, max(0, int((d["at"] - t0) // SPARK_BIN_S)))].append(v)
                row["rate"] = [_r(median(x)) for x in r]
            rows.append(row)
        out = {"from": t0, "to": now, "bin_s": SPARK_BIN_S, "subjects": rows}
        with self._lock:
            self._spark = (now, out)
        return out

    def view(self, name: str, now: float | None = None) -> dict:
        """GET /api/cad/metrics?view=squelch | spark | shifts. MetricsError-free: an unknown view is a ValueError."""
        if name == "squelch":
            return self.squelch(now)
        if name == "spark":
            return self.spark(now)
        if name == "shifts":
            return {"shifts": self.found, "at": self.turned_at, "settings": self.cfg()}
        raise ValueError("view: squelch, spark or shifts")
