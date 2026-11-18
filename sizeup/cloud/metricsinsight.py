"""The station's signal metrics read back (#606, on top of #605's sizeup/cloud/metricstore.py): a squelch suggested per
channel from weeks of its own noise and traffic, the shifts after an event (a rewire, a dongle moved, a restart) as
Health warnings, and the compact 24 h lines Health draws. The hub's; admins only (GET /api/cad/metrics?view=...).

Suggested squelch (suggest(); FM traffic channels can apply it, paging and AM get it for information only). It must
never make a channel worse: when in doubt, the running Open stays and Apply isn't offered.

    window     the last SUGGEST_DAYS, or from the channel's last RF event (rf_events(): a dongle moved, a gain; back
               from Off or plugged back in only on another dongle than before) when that is nearer: what it heard
               before says nothing about now. Under MIN_FLOOR_MIN minutes (24 h) in the window: "not enough data
               (since <event>)"
    noise      the p90 of each idle minute's q90 (the squelch closed the whole minute, open_s == 0): how far the hiss
               alone quiets itself on nine minutes in ten; the idle max: the p90 of each idle minute's max
    open       ceil(noise + margin_db) (Settings, default 3 dB), clamped to the Squelch card's range
    weak       the real transmissions in the window (voice, an ID burst, a page: never the "noise" kind), each at its
               quieting at open ("qo", else "q"). By default (miss_pct 0) none may be under the Open: more than miss_pct
               under it and the cap (their percentile - WEAK_GAP_DB) lands under noise + margin: "weak traffic overlaps
               the noise", the running Open stays, no Apply
    raise      only with MIN_REAL_TX real transmissions to check it against; one that would still leave any under it:
               no Apply (the count in the warning). A raised Close stays under the real tails' quieting at close (qc)
    lower      never into the idle max, and at most MAX_LOWER_DB a step (look again after)
    chatter    the noise opens' median since the last squelch change (an hour of minutes at least): at CHATTER_PER_MIN or
               more the idle reads low (an excursion over the Open opens it, so that minute isn't idle), so it is never
               suggested lower, and +1 dB. For SETTLING_S after a squelch change: no chatter rule, no Apply
    close      open minus the running hysteresis (Open - Close), in its range, under the open
    paging     the same numbers, information only: its squelch is read-only (a high Open would mute the pages); never
               above the running Open, and capped PAGING_GAP_DB (not WEAK_GAP_DB) under its weak pages
    Apply      sends what it was worked out for (expect: the suggestion's own cur_open / cur_close); the hub refuses it
               if another runs now, or if the channel's suggestion no longer offers Apply (Insight.applicable())
    restart    a station squelch event within SETTLE_S of its restart that only puts back what ran before it is no
               squelch change (restart_echoes(): no settling after every restart)
    limit      the quieting is measured against the receiver's own hiss reference, which follows the floor (radio/fm.py):
               a uniform rise of the noise moves both and can read as less than it is. The AM floor (dBFS) and the
               transmissions' own levels (the "tx_q" shift) don't have that limit

Shift warnings (shifts(); the hub's janitor runs it every 10 min, Health shows the result as "metrics_shift" hints,
never a push; mutable like the alerts, Settings > System > Push notifications):

    after an event (EVENTS: the station's sdr / gain / squelch / unplug / back / on / start, or the Settings audit's
    squelch change to the channel) in the last WARN_S, the AFTER_S after it (from its first SETTLE_S, at least
    MIN_AFTER_S of data, never past the next event) against the BEFORE_S before it (never before the event before);
    with later events, what it found must still show from the newest one on (a dongle moved back: no warning; a squelch
    change since: the idle checks can't tell any more):
        FM       the idle minutes' q90 median up more than shift_db; the noise opens a minute (the median of half-hour
                 means, over OPENS_BINS half hours at least: one burst of static is one half hour) up opens_x times and
                 at least OPENS_MIN (neither after a squelch change alone: the Open itself moves both; with an SDR
                 change minutes before or after it, they are checked); a traffic channel's kept voice quieting (the
                 median of half-hour medians, TXQ_MIN each side, over OPENS_BINS half hours: a fire's hour of portables
                 never moves it) down more than shift_db (a cable's or a splitter's loss; a restart too: what was
                 changed while the station was down shows only as its restart); too few half hours with traffic: an
                 info hint "level not checked" instead
        AM       the floor (dBFS) median up more than shift_db
        traffic  the transmissions an hour (the median of half-hour counts: a fire's hour of traffic moves it
                 little) against the same clock hours the day before (a quiet night after a busy evening is no shift):
                 rate_x times fewer, from at least RATE_MIN; or the kept noise key-ups rate_x times more, to at least
                 RATE_MIN (what a gain, a squelch or interference brings)
        paging   the pages' quieting at open, median down more than shift_db over at least PAGES_MIN pages after
                 (windows PAGE_BEFORE_S / PAGE_AFTER_S: pages are few; never from before the event before)
        P25      the control channel's decode rate median down more than cc_drop_pct %
    with no event, a drift check once a day: the last DRIFT_S's hourly medians against the DRIFT_BEFORE_S before, the
    same thresholds (FM idle q90, AM floor, P25 decode rate). A step a day old moves neither median: no warning
    without an event, until it has lasted most of a week.

Bounded: each check reads one series' raw records for at most a day and a quarter (pages: ten days), at most
MAX_EVENTS events a subject; the suggestions are kept CACHE_S. Nothing here runs on the alert path.
"""

from __future__ import annotations

import math
import re
import threading
from urllib.parse import quote
import time
from typing import Callable

from sizeup import tz

SECTION = "signal_metrics"     # the CAD settings section (sizeup/cad/settings.py), audited as Radios
DEFAULTS = {"margin_db": 3.0, "miss_pct": 0.0, "shift_db": 3.0, "opens_x": 3.0, "rate_x": 3.0, "cc_drop_pct": 30.0}
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
MAX_LOWER_DB = 2.0             # a lower Open: at most this much a step
SETTLING_S = 6 * 3600.0        # after a squelch change: no Apply, no chatter rule, this long
RF_EVENTS = ("sdr", "gain")   # what changes what a channel hears: its suggestion starts after (and rf_events())
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
TXQ_MIN = 8                    # kept voice transmissions each side, for their level
OPENS_BINS = 3                 # half hours after an event, for the noise opens' median
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

def _when(t: float, now: float) -> str:
    """"14:05" today, "Nov 15 14:05" on another day (the console's time zone)."""
    if tz.clock(t, "%Y-%m-%d") == tz.clock(now, "%Y-%m-%d"):
        return tz.hhmm(t)
    return f"{tz.clock(t, '%b')} {int(tz.clock(t, '%d', '0'))} {tz.hhmm(t)}"


def suggest(floor: list[dict], tx: list[dict], cfg: dict, now: float, span_s: float = SUGGEST_DAYS * 86400.0,
            since: dict | None = None, squelch_at: float | None = None) -> dict:
    """A channel's suggested squelch from its floor and tx records (oldest first) over its window: the last span_s, or
    from `since` (the last event that changed what the channel hears: {at, ev}) when that is nearer. squelch_at: the
    last squelch change (the chatter is counted from it; Apply waits SETTLING_S after it).

    Never makes a channel worse: when in doubt the running Open stays and Apply isn't offered. {state: "ok" | "short",
    open, close, apply, info (why it is information only: paging, AM), warn (why it isn't offered: weak traffic overlaps
    the noise, a raise would miss transmissions, settling), the numbers it used, why (one line), hist}."""
    cfg = settings(cfg)
    st = _settings_now(floor)
    kind = str(st.get("kind") or "")
    am = bool(st.get("am"))
    paging = kind == "paging"
    cur_o, cur_c = _num(st.get("open")), _num(st.get("close"))
    idle = [d for d in floor if _num(d.get("q90")) is not None and not d.get("open_s")]
    q90 = [float(d["q90"]) for d in idle]
    qmax = [float(d["qmax"]) for d in idle if _num(d.get("qmax")) is not None]
    real = [d for d in tx if d.get("kind") in REAL_KINDS]
    rq = [v for d in real if (v := _num(d.get("qo") if d.get("qo") is not None else d.get("q"))) is not None]
    rc = [v for d in real if (v := _num(d.get("qc"))) is not None]
    nq = [v for d in tx if d.get("kind") == "noise" and (v := _num(d.get("q"))) is not None]
    sq_from = (squelch_at + SETTLE_S) if squelch_at is not None else None
    opens = [v for d in floor if (sq_from is None or d["at"] >= sq_from) and (v := _num(d.get("noise"))) is not None]
    days = round(min(span_s, now - since["at"]) / 86400.0 if since else span_s / 86400.0, 1)
    out: dict = {"subject": "", "kind": kind, "am": am, "minutes": len(floor), "idle_min": len(idle), "real": len(rq),
                 "noise_tx": len(nq), "cur_open": cur_o, "cur_close": cur_c, "margin_db": cfg["margin_db"],
                 "miss_pct": cfg["miss_pct"], "days": days, "warn": "", "info": "", "apply": False,
                 "since": {"at": since["at"], "ev": since["ev"]} if since else None,
                 "hist": {"idle": _hist(q90), "real": _hist(rq), "noise": _hist(nq)}}
    if len(floor) < MIN_FLOOR_MIN or len(idle) < MIN_IDLE_MIN:
        hrs = len(floor) / 60.0
        out.update(state="short", why=(f"not enough data since {EVENTS.get(since['ev'], since['ev'])} {_when(since['at'], now)}"
                                       if since else "not enough data") + f": {hrs:.0f} h of {MIN_FLOOR_MIN / 60:.0f} h"
                   + (f", {len(idle)} idle min" if len(floor) >= MIN_FLOOR_MIN else ""))
        return out
    noise = pct(q90, 90)
    nmax = pct(qmax, 90) if qmax else noise
    out["noise_p90"], out["noise_max_p90"] = _r(noise), _r(nmax)
    base = float(math.ceil(noise + cfg["margin_db"] - 1e-9))
    out["base"] = base
    gap = PAGING_GAP_DB if paging else WEAK_GAP_DB
    notes: list[str] = []
    warn = ""
    chatter = median(opens) if len(opens) >= 60 else None    # an hour of minutes since the last squelch change
    out["opens_p50"] = _r(chatter, 2)
    settling = squelch_at is not None and now - squelch_at < SETTLING_S
    chattering = chatter is not None and chatter >= CHATTER_PER_MIN and not settling
    want = base
    if chattering and cur_o is not None and want < cur_o + 1:
        want = cur_o + 1.0
        notes.append(f"chattering {chatter:g}/min: idle reads low; +1 dB, look again")
    lo, hi = OPEN_RANGE
    want = min(max(want, lo), hi)
    capped = False
    under = sum(1 for v in rq if v < want)
    allowed = len(rq) * cfg["miss_pct"] / 100.0
    if rq and under > allowed:
        weak = pct(rq, cfg["miss_pct"]) if cfg["miss_pct"] > 0 else min(rq)
        out["weak"] = _r(weak)
        cap = _step_down(weak - gap)
        if cap < base:
            # the weakest real traffic sits in the noise (+ margin): no Open serves both; the one running stays
            warn = f"weak traffic overlaps the noise: {under} of {len(rq)} real under {want:g}"
            want = cur_o if cur_o is not None else want
        else:
            want, capped = cap, True
            notes.append(f"capped under weak traffic: {under} of {len(rq)} were under {base:g}")
    elif rq:
        out["weak"] = _r(pct(rq, cfg["miss_pct"]) if cfg["miss_pct"] > 0 else min(rq))
    if cur_o is not None and not warn:
        if want < cur_o:
            if chattering:
                want = cur_o                               # chattering: never lower
            else:
                # lower: never into the idle max, at most MAX_LOWER_DB a step (look again after)
                floor_max = _step_up(nmax + STEP)
                want = max(want, floor_max, cur_o - MAX_LOWER_DB)
                if want >= cur_o:
                    want = cur_o
                elif want > base:
                    notes.append(f"lowered at most {MAX_LOWER_DB:g} dB a step, over the idle max {nmax:.1f}")
        elif want > cur_o:
            if paging:
                notes.append("paging: never above the running Open")
                want = cur_o
            elif len(rq) < MIN_REAL_TX:
                notes.append(f"{len(rq)} real tx: too few to check a raise")
                want = cur_o
    out["under"] = sum(1 for v in rq if v < want)
    if not warn and cur_o is not None and want > cur_o and out["under"] > allowed:
        warn = f"a raise to {want:g} would miss {out['under']} of {len(rq)} real transmissions"
    hyst = (cur_o - cur_c) if cur_o is not None and cur_c is not None and cur_o > cur_c else 4.0
    close = min(CLOSE_RANGE[1], max(CLOSE_RANGE[0], _step_down(want - hyst)))
    if cur_c is not None and close > cur_c and rc:
        # a raised Close cuts a transmission's tail sooner: never above the real tails' own quieting at close
        tail = _step_down(min(rc) - gap)
        if close > max(tail, cur_c):
            close = max(tail, cur_c)
            notes.append(f"Close held under the tails (qc min {min(rc):.1f})")
    if close >= want:
        close = want - STEP
    out["cut_tails"] = sum(1 for v in rc if v < close) if cur_c is not None and close > cur_c else 0
    out.update(state="ok", open=_r(want), close=_r(close), capped=capped,
               same=cur_o is not None and abs(want - cur_o) < STEP / 2 and (cur_c is None or abs(close - cur_c) < STEP / 2))
    out["info"] = "paging: read-only" if paging else "AM: no squelch setting here" if am else \
        "" if kind in ("traffic", "") else f"{kind}: read-only"
    if settling and not warn:
        warn = f"settling: squelch changed {_when(squelch_at, now)}"
    out["warn"] = warn
    out["apply"] = not out["info"] and not warn and not out["same"] and cur_o is not None
    out["why"] = (f"noise p90 {noise:.1f} dB, idle max p90 {nmax:.1f} ({len(idle):,} idle min, {days:g} d"
                  + (f" since {EVENTS.get(since['ev'], since['ev'])}" if since else "") + f") + {cfg['margin_db']:g} "
                  f"→ {base:g} · {out['under']}/{len(rq)} real under {want:g}"
                  + ((f" · weak p{cfg['miss_pct']:g} {out['weak']:.1f}" if cfg["miss_pct"] else f" · weakest {out['weak']:.1f}")
                     if "weak" in out else "")
                  + "".join(f" · {n}" for n in notes) + (f" · {warn}" if warn else ""))
    return out


# -- the shifts -------------------------------------------------------------------------------------------------------

def _serial(v) -> str | None:
    """A dongle's serial as an event or a floor record names it; None when it names none that tells dongles apart
    (none, "", a device index "0".."99": the station's _serial() gives the configured string when it looked up no
    serial; an RTL-SDR's serial is 8 characters, often all digits: "00000102")."""
    v = str(v or "").strip()
    return None if not v or v == "none" or (v.isdigit() and len(v) <= 3) else v


def _sdr_of(ev: dict) -> str | None:
    """The dongle an event names: "sdr" (SDR a -> b): b; "on" / "back" (SDR x): x; None when it names none."""
    d = str(ev.get("detail") or "")
    if not d.startswith("SDR "):
        return None
    return _serial(d.rsplit("->", 1)[-1] if ev.get("ev") == "sdr" else d[4:])


def rf_events(evs: list[dict], before: Callable[[float], str | None] | None = None) -> list[dict]:
    """Of a channel's events (oldest first), those that change what it hears: an SDR change, a gain change, a station
    restart marked "rf" (Insight._restarts(): its dongle or gain differs from before it; the station's first look
    after a restart is never an event, so that is the only trace of a change made while it was down); "on" (back from
    Off: a shared dongle handed over, every few hours) and "back" (plugged in again) only on another dongle than the
    one before, or when either is unknown (no serial: a device index), or marked "rf" (Insight._restarts(): its gain
    moved) (#686: a channel handed over every day on the same dongle still gets one). before(t): the dongle the floor records name before t, for the first of them in the
    window. "unplug" never on its own: the dongle that comes back says whether anything changed."""
    out: list[dict] = []
    cur: str | None = None
    for e in evs:
        ev = e.get("ev")
        if ev in ("on", "back"):
            s = _sdr_of(e)
            if cur is None and before is not None and isinstance(e.get("at"), (int, float)):
                cur = _serial(before(e["at"]))
            if s is None or cur is None or s != cur or e.get("rf"):
                out.append(e)
            cur = s
        elif ev == "start":
            if e.get("rf"):
                out.append(e)
            cur = _serial(e.get("sdr")) or cur
        elif ev in RF_EVENTS:
            out.append(e)
            if ev == "sdr":
                cur = _sdr_of(e)
    return out


_SQ = re.compile(r"^open (-?[\d.]+) close (-?[\d.]+)")


def restart_echoes(evs: list[dict], before: Callable[[float], dict]) -> list[dict]:
    """evs without the station's squelch events that only put back what ran before a restart (#686): one within
    SETTLE_S after the station's "start" whose Open / Close are what the floor records said ran just before it
    (before(t): the settings in force at t). The station keeps the hub's squelch and runs it from its first look, so
    a restart logs none; one whose own Settings store lost it would log the hub's push as a change, and 6 h of
    settling would follow every restart."""
    starts = [e["at"] for e in evs if e.get("ev") == "start" and isinstance(e.get("at"), (int, float))]
    if not starts:
        return evs
    out = []
    for e in evs:
        if e.get("ev") == "squelch" and e.get("source", "station") == "station" and isinstance(e.get("at"), float | int):
            st = max((t for t in starts if 0 <= e["at"] - t <= SETTLE_S), default=None)
            m = _SQ.match(str(e.get("detail") or ""))
            if st is not None and m:
                was = before(st) or {}
                o, c = _num(was.get("open")), _num(was.get("close"))
                if o is not None and c is not None and abs(o - float(m[1])) < 0.01 and abs(c - float(m[2])) < 0.01:
                    continue
        out.append(e)
    return out


def _event_name(ev: dict) -> str:
    return EVENTS.get(ev.get("ev"), str(ev.get("ev") or "event"))


def _is_squelch(ev: dict) -> bool:
    return ev.get("ev") == "squelch" or (ev.get("ev") == "setting" and "squelch/" in str(ev.get("detail") or ""))


def clusters(events: list[dict]) -> list[dict]:
    """The events that can explain a shift (EVENTS; of the Settings audit's, a squelch change), oldest first, those within MERGE_S of the first of a run merged
    into it: {at, ev, names, squelch (one of them changed the squelch), rf (one of them is something else: an SDR
    change with a squelch edit minutes later is still checked as an SDR change), detail}."""
    out: list[dict] = []
    for e in sorted((e for e in events if e.get("ev") in EVENTS and isinstance(e.get("at"), (int, float))
                     and (e["ev"] != "setting" or _is_squelch(e))), key=lambda e: e["at"]):   # (record / transcribe: no)
        if out and e["at"] - out[-1]["at"] <= MERGE_S:
            c = out[-1]
            if _event_name(e) not in c["names"]:
                c["names"].append(_event_name(e))
            c["squelch"] = c["squelch"] or _is_squelch(e)
            c["rf"] = c["rf"] or not _is_squelch(e)
            continue
        out.append({"at": float(e["at"]), "ev": e["ev"], "names": [_event_name(e)], "squelch": _is_squelch(e),
                    "rf": not _is_squelch(e), "detail": str(e.get("detail") or "")[:120], "by": e.get("by") or ""})
    return out


def _vals(docs: list[dict], key: str, idle: bool = False) -> list[float]:
    return [v for d in docs if (not idle or not d.get("open_s")) and (v := _num(d.get(key))) is not None]


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _half_hours(docs: list[dict], key: str) -> list[float]:
    """The mean of `key` in each half hour that has records (at least 10 of them)."""
    bins: dict[int, list[float]] = {}
    for d in docs:
        if (v := _num(d.get(key))) is not None:
            bins.setdefault(int(d["at"] // RATE_BIN_S), []).append(v)
    return [sum(v) / len(v) for v in bins.values() if len(v) >= 10]


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
        # the median of half-hour means: one burst of static after a restart is one half hour, never the median
        bh, ah = _half_hours(before, "noise"), _half_hours(after, "noise")
        bo, ao = median(bh), median(ah)
        if bo is not None and ao is not None and len(ah) >= OPENS_BINS and ao >= OPENS_MIN and ao >= cfg["opens_x"] * bo:
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


def _level(docs: list[dict], kinds: tuple, key: str, binned: bool) -> tuple[float | None, int, int]:
    """The transmissions' level: (the median, or binned: the median of each half hour's median; their count; the
    half hours)."""
    bins: dict[int, list[float]] = {}
    for d in docs:
        if d.get("kind") in kinds and (v := _num(d.get(key))) is not None:
            bins.setdefault(int(d["at"] // RATE_BIN_S), []).append(v)
    vals = [v for b in bins.values() for v in b]
    if not binned:
        return median(vals), len(vals), len(bins)
    return median([median(b) for b in bins.values()]), len(vals), len(bins)


def check_levels(btx: list[dict], atx: list[dict], cfg: dict, paging: bool) -> list[dict]:
    """The transmissions' own level, before against after: a paging channel's pages at open (a splitter's loss), a
    traffic channel's kept voice quieting (a cable's or a splitter's loss: every carrier weaker). Traffic: the median
    of half-hour medians, over OPENS_BINS half hours each side at least (an hour of portables on a working fire is two
    half hours, never the median, #686); pages are few: their median."""
    kinds, key, n_min = (("page",), "qo", PAGES_MIN) if paging else (("voice", "burst"), "q", TXQ_MIN)
    b, nb, hb = _level(btx, kinds, key, not paging)
    a, na, ha = _level(atx, kinds, key, not paging)
    if not paging and nb >= n_min and na >= n_min and (hb < OPENS_BINS or ha < OPENS_BINS):
        # traffic on both sides, but bunched (one fire's portables): its level says nothing either way; said, not silent
        return [{"metric": "tx_q_unchecked", "level": "info", "before": None, "after": None,
                 "text": "level not checked: too little traffic",
                 "title": f"kept voice in {hb} half hours before, {ha} after ({nb} / {na} tx): needs {OPENS_BINS} each"}]
    if nb >= n_min and na >= n_min and (paging or (hb >= OPENS_BINS and ha >= OPENS_BINS)):
        if b - a > cfg["shift_db"]:
            if paging:
                return [{"metric": "tx_q_open", "before": _r(b), "after": _r(a),
                         "text": f"pages open at {a:.1f} dB (was {b:.1f}, {na} pages)"}]
            return [{"metric": "tx_q", "before": _r(b), "after": _r(a),
                     "text": f"transmissions at {a:.1f} dB (was {b:.1f}, {na} tx)"}]
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
        """The channel's suggestion over its window: the last SUGGEST_DAYS, or from its last RF event (rf_events(): a
        dongle moved, a gain: what it hears changed, so what came before says nothing about now). Kept CACHE_S, and
        worked out again at once when a newer event arrives (the key has the newest event's time)."""
        now = self.clock() if now is None else now
        t0 = now - SUGGEST_DAYS * 86400.0
        evs = self._restarts(station, subject, self._events(station, subject, t0, now + 60), now)
        evs = [e for e in evs if e.get("subject") == subject or e.get("source") == "audit" or e.get("rf")]
        sb = getattr(self.store, "settings_before", None)
        rf = rf_events(evs, (lambda t: sb(station, subject, t).get("sdr")) if sb is not None else None)
        sq = [e["at"] for e in evs if _is_squelch(e)]
        newest = max((e["at"] or 0 for e in evs), default=0.0)
        key = (station, subject)
        with self._lock:
            hit = self._sugg.get(key)
        if hit and now - hit[0] < CACHE_S and hit[1].get("_ev") == newest:
            return hit[1]
        since = {"at": rf[-1]["at"], "ev": rf[-1]["ev"]} if rf else None
        w0 = max(t0, since["at"] + SETTLE_S) if since else t0      # (read outside the lock: Health's sparks never wait)
        floor = self.store.docs(station, subject, "floor", w0, now + 60, MAX_DOCS, newest=True)
        tx = self.store.docs(station, subject, "tx", w0, now + 60, MAX_DOCS, newest=True)
        out = suggest(floor, tx, self.cfg(), now, since=since, squelch_at=sq[-1] if sq else None)
        out.update(subject=subject, station=station, at=now, _ev=newest)
        with self._lock:
            self._sugg[key] = (now, out)
        return out

    def _events(self, station: str, subject: str, t0: float, t1: float) -> list[dict]:
        """The store's events without a restart's echo of the squelch (restart_echoes())."""
        evs = self.store.events(station, subject, t0, t1)
        before = getattr(self.store, "settings_before", None)
        if before is None or not any(e.get("ev") == "start" for e in evs):
            return evs
        return restart_echoes(evs, lambda t: before(station, subject, t))

    def _restarts(self, station: str, subject: str, evs: list[dict], now: float) -> list[dict]:
        """evs with each station restart marked "rf" when the channel's dongle or gain after it differs from before it:
        a dongle swapped, a gain changed in the config or a splitter added while the station was down never sends
        "sdr" or "gain" (#686). After: its first floor settings up to the next restart (a channel that runs hours
        later: a shared dongle held elsewhere, absent, auto-assign); before: the last ones in the SUGGEST_DAYS before
        (a long outage); none before but some after: marked too (a day's wait after it, never the old dongle's days).
        "sdr": the dongle after it. An "on" / "back" is marked "rf" when only the gain moved (rf_events() compares the
        dongles)."""
        sb = getattr(self.store, "settings_before", None)
        if sb is None:
            return evs
        starts = sorted(e["at"] for e in evs if e.get("ev") == "start" and isinstance(e.get("at"), (int, float)))
        out = []
        for e in evs:
            ev, at = e.get("ev"), e.get("at")
            if ev in ("start", "on", "back") and isinstance(at, (int, float)):
                t1 = min([t for t in starts if t > at] + [now + 60])
                after = next((d for d in self.store.docs(station, subject, "floor", at, t1, 60)
                              if "sdr" in d or "gain" in d), {})
                was = sb(station, subject, at, SUGGEST_DAYS * 86400.0)
                keys = ("sdr", "gain") if ev == "start" else ("gain",)
                moved = any(k in was and k in after and was[k] != after[k] for k in keys)
                if ev == "start":
                    unknown = not any(k in was for k in keys) and any(k in after for k in keys)
                    e = dict(e, rf=moved or unknown, sdr=after.get("sdr"))
                elif moved:
                    e = dict(e, rf=True)
            out.append(e)
        return out

    def _live(self, now: float) -> list[tuple[str, str]]:
        """(station, subject) of each series with floor records in the suggestion's window (SUGGEST_DAYS): an old or
        renamed station id still in the 30 days kept never stands beside the one that reports the channel now (#686)."""
        t0 = now - SUGGEST_DAYS * 86400.0
        return sorted({(s["station"], s["subject"]) for s in self.store.index()["series"] if s["kind"] == "floor"
                       and s["subject"] != "station" and (s.get("last") is None or s["last"] >= t0)})

    def applicable(self, subject: str, expect: dict, target: dict | None = None, now: float | None = None) -> str:
        """An Apply from the Squelch card, checked again here (#686): "" when the channel's suggestion still offers
        Apply, was worked out for the Open / Close in `expect` (the suggestion's own cur_open / cur_close) and still
        suggests the Open / Close in `target` (what the Apply sets: a confirm left open while the noise rose would
        otherwise set an Open the suggestion has moved past), else why not."""
        now = self.clock() if now is None else now
        rows = [self.suggestion(st, s, now) for st, s in self._live(now) if s == subject]
        rows = [r for r in rows if r.get("minutes")]
        if len(rows) != 1:
            return f"{subject}: {'no suggestion' if not rows else 'two stations report it'} now, look again"
        s = rows[0]
        if not s.get("apply"):
            why = s.get("warn") or s.get("info") or s.get("why") or "no change"
            return f"{subject}: the suggestion isn't offered now ({why})"
        for f, k in (("open_db", "cur_open"), ("close_db", "cur_close")):
            e, c = _num((expect or {}).get(f)), _num(s.get(k))
            if e is None or c is None or abs(e - c) > 0.01:
                return (f"{subject}: the suggestion was worked out for Open {s.get('cur_open')} / Close "
                        f"{s.get('cur_close')}, look again")
        for f, k in (("open_db", "open"), ("close_db", "close")):
            e, c = _num((target or {}).get(f)), _num(s.get(k))
            if e is None or c is None or abs(e - c) > 0.01:
                return f"{subject}: the suggestion is now Open {s.get('open')} / Close {s.get('close')}, look again"
        return ""

    def forget(self, subject: str) -> None:
        """A channel's squelch changed (Settings): its suggestion worked out again at the next look."""
        with self._lock:
            for k in [k for k in self._sugg if k[1] == subject]:
                del self._sugg[k]

    def renamed(self, old: str, new: str) -> None:
        """A station renamed (its rows moved to `new`, sizeup/cloud/hub.py apply_station_renames): what was worked out
        under `old` is dropped (looked up again under `new`), and the shifts in force say `new`."""
        with self._lock:
            for k in [k for k in self._sugg if k[0] == old]:
                del self._sugg[k]
            self._spark = None
            self.found = [dict(f, station=new) if f.get("station") == old else f for f in self.found]
            self._drift = [dict(f, station=new) if f.get("station") == old else f for f in self._drift]

    def squelch(self, now: float | None = None) -> dict:
        """view=squelch: each FM / AM channel's suggestion (Settings > Radios > Squelch and Signal); a station with no
        floor records of it in the window left out (_live)."""
        now = self.clock() if now is None else now
        rows = [self.suggestion(st, s, now) for st, s in self._live(now)]
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
        evs = clusters(self._events(station, subject, now - WARN_S - BEFORE_S, now + 60))
        recent = [c for c in evs if c["at"] >= now - WARN_S][-MAX_EVENTS:]
        out: dict[str, dict] = {}
        meta = self._meta(station, subject, now) if "floor" in kinds else {}
        def run(b0: float, b1: float, a0: float, a1: float, p1: float, squelch: bool, prev: list[float],
                avoid: list[float]) -> list[dict]:
            """prev: the events before b1 (the windows start after them); avoid: every event time (the rates'
            same-hours-yesterday window must hold none)."""
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
                same = not any(y0 - SETTLE_S <= p <= y1 for p in avoid)
                tb = self.store.docs(station, subject, "tx", y0, y1) if same else None
                ta = self.store.docs(station, subject, "tx", a0, a1)
                hits += check_channel(fb, fa, tb, ta, (y0, y1), (a0, a1), meta, cfg, squelch)
                if not meta.get("am"):              # the level: from the event before at most (never another's)
                    l0 = max(b1 - PAGE_BEFORE_S, prev[-1] + SETTLE_S if prev else 0.0)
                    hits += check_levels(self.store.docs(station, subject, "tx", l0, b1),
                                         self.store.docs(station, subject, "tx", a0, p1), cfg, meta.get("kind") == "paging")
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
            alls = [p["at"] for p in evs]
            hits = run(b0, c["at"], a0, a1, p1, c["squelch"] and not c["rf"], prev, alls)
            if hits and later:
                # still so now? from the newest event on (a dongle moved back: no); a squelch change since: the idle
                # can't tell any more, so only what it still shows stays
                s0 = later[-1]["at"] + SETTLE_S
                if now - s0 >= MIN_AFTER_S:
                    still = {h["metric"] for h in run(b0, c["at"], s0, now, now, (c["squelch"] and not c["rf"])
                                                      or any(p["squelch"] for p in later), prev, alls)}
                    hits = [h for h in hits if h["metric"] in still]
            for h in hits:
                if h["metric"] in out:
                    continue                          # a newer event explains it already
                out[h["metric"]] = {**h, "subject": subject, "station": station, "at": c["at"], "event": c["ev"],
                                    "names": c["names"], "detail": c["detail"], "by": c["by"],
                                    "text": f"{h['text']} since {_when(c['at'], now)} ({', '.join(c['names'])})"}
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
        return [{"channel": f["subject"], "level": f.get("level") or "warn", "kind": "metrics_shift", "text": f["text"],
                 "at": f["at"], **({"title": f["title"]} if f.get("title") else {}),
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
            if subject == "station" or not kinds & {"floor", "cc"}:      # (a channel's STT input arrival only: #677)
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
