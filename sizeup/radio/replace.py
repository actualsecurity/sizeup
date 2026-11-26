"""A dongle swapped for a new one: the gone dongle's jobs offered to a new dongle that fits them.

2026-11-25: new RTL-SDR dongles (new serials) went in, the channels stayed on the old serials (most in no antenna
group, so nothing moved them) and coverage was lost for hours. Health said "SDR <serial> gone" with no word that the
new dongles sat there unused. Now the station says so at once (sizeup/app.py replace_offers(), its sdr state's
"replace"), Health and the console ask, and a person's "Use it" moves the jobs (App.replace_sdr).

What is offered (all of it decided here, from what the app hands in):
    missing    a dongle with a job, or in an antenna group, not plugged in for MISSING_S
    new        a dongle plugged in with no job, in no group, not switched Off by a person, not another app's, not
               out for no audio, not lent to a reception test, its serial on one dongle
    fits       the new dongle can do every job the missing one ran (fits()): each frequency in its tuner's range
               (R820T / R828D / E4000 / FC0012 / FC0013 / FC2580), HF only with direct sampling (a Blog V3) or the
               V4's upconverter, the sample rate the job takes
    pairing    several missing and several new: the pairs with the best total match (pair()): the same tuner first,
               then the same product, then the same USB port (or bus) the missing one was on. A missing dongle whose
               best new one isn't decided by that is asked about: its offer lists the choices, none picked.

The tuner: the one rtl_fm reported when it opened the dongle ("Found Rafael Micro R820T tuner", kept per serial),
else what the USB product says ("Blog V4" is an R828D with the HF upconverter), else unknown: an unknown tuner is
taken as the common R820T (24-1766 MHz) and matched below a known one, and never for HF.

Pure functions: no dongle is opened here, nothing is written.
"""

from __future__ import annotations

MISSING_S = 60.0             # a dongle gone this long (in this run) has its jobs offered to a new one
LONG_GONE_S = 24 * 3600.0    # a group's dongle missing this long: Health asks whether to take it out of the group
NOT_NOW_S = 6 * 3600.0       # "Not now": that missing dongle's offer is not made again for this long
RATE_MAX = 2.56e6            # every RTL2832U without dropped samples
HF_MHZ = 24.0                # under it: direct sampling (Blog V3) or the V4's upconverter

# Each tuner's tuning range, MHz (librtlsdr's, rounded to the datasheets)
TUNERS = {
    "R820T": ((24.0, 1766.0),),
    "R828D": ((24.0, 1766.0),),
    "E4000": ((52.0, 1100.0), (1250.0, 2200.0)),
    "FC0012": ((22.0, 948.6),),
    "FC0013": ((22.0, 1100.0),),
    "FC2580": ((146.0, 308.0), (438.0, 924.0)),
}
ASSUMED = "R820T"            # an unknown tuner is taken as this one


def tuner_name(text: str) -> str:
    """"Rafael Micro R820T" / "R820T2" / "Elonics E4000" -> the TUNERS key, or ""."""
    t = str(text or "").upper().replace(" ", "")
    for k in ("R828D", "R820T", "E4000", "FC0012", "FC0013", "FC2580"):
        if k in t:
            return k
    return "E4000" if "E4K" in t else ""


def caps(product: str = "", tuner: str = "") -> dict:
    """What a dongle can do: {"tuner": a TUNERS key or "", "known": the tuner was reported or named, "hf": HF works
    (direct sampling or an upconverter), "bands": its ranges, "product"}."""
    p = str(product or "")
    low = p.lower()
    t = tuner_name(tuner) or tuner_name(p)
    if not t and "blog v4" in low:
        t = "R828D"                              # the RTL-SDR Blog V4: R828D, HF through its upconverter
    elif not t and ("blog v3" in low or "rtl-sdr blog" in low):
        t = "R820T"                              # the Blog V3: R820T2, HF by direct sampling (Q branch)
    hf = "blog" in low
    return {"tuner": t, "known": bool(t), "hf": hf, "bands": TUNERS.get(t or ASSUMED, TUNERS[ASSUMED]), "product": p}


def needs(mhz=(), rate: float = 0.0) -> dict:
    """A job's needs: {"mhz": its frequencies, "rate": the sample rate it takes (S/s, 0 = rtl_fm's own, small),
    "hf": any frequency under HF_MHZ}."""
    fs = sorted({round(float(f), 4) for f in mhz if f})
    return {"mhz": fs, "rate": float(rate or 0.0), "hf": any(f < HF_MHZ for f in fs)}


def fits(c: dict, need: dict) -> str:
    """"" when a dongle with caps `c` can run a job with `need`, else why not (short, for a hover)."""
    t = c.get("tuner") or ASSUMED
    if need.get("rate", 0.0) > RATE_MAX:
        return f"needs {need['rate'] / 1e6:g} MS/s"
    for f in need.get("mhz") or ():
        if f < HF_MHZ:
            if not c.get("hf"):
                return f"{f:g} MHz needs direct sampling (Blog V3/V4)"
            continue
        if not any(lo <= f <= hi for lo, hi in c.get("bands") or TUNERS[ASSUMED]):
            return f"{f:g} MHz outside the {t}" + ("" if c.get("known") else " (assumed)")
    if need.get("hf") and not c.get("hf"):
        return "needs HF"
    return ""


def score(old: dict, new: dict) -> int:
    """How alike the new dongle is to the missing one: the same known tuner 4, the same product 2, the same USB port
    3, else the same bus 1 (old / new: {"caps", "port", "bus"})."""
    a, b = old.get("caps") or {}, new.get("caps") or {}
    s = 0
    if a.get("known") and b.get("known") and a.get("tuner") == b.get("tuner"):
        s += 4
    elif not a.get("known") and b.get("known") and b.get("tuner") == ASSUMED:
        s += 1                                   # (the missing one's tuner never seen: the common one first)
    if a.get("product") and a.get("product") == b.get("product"):
        s += 2
    if old.get("port") and old.get("port") == new.get("port"):
        s += 3
    elif old.get("bus") and old.get("bus") == new.get("bus"):
        s += 1
    return s


MAX_PAIRING = 8              # more missing dongles than this: matched one by one, best first (never seen)


def pair(missing: list[dict], new: list[dict]) -> list[dict]:
    """The offers. missing: [{"serial", "needs": [job needs], "caps", "port", "bus", ...}], new: [{"serial", "caps",
    "port", "bus", ...}]. Each missing dongle with at least one new dongle that fits it gets an offer:
    {"missing", "choices": [new serials that fit, best first], "pick": the one the pairing decided, or "" (ask),
    "only": exactly one new dongle fits it and it fits no other missing dongle (unambiguous), "why": {new serial:
    why it doesn't fit}}."""
    fit = {m["serial"]: {} for m in missing}
    why: dict[str, dict[str, str]] = {m["serial"]: {} for m in missing}
    for m in missing:
        for n in new:
            no = next((w for nd in m.get("needs") or [] if (w := fits(n.get("caps") or {}, nd))), "")
            if no:
                why[m["serial"]][n["serial"]] = no
            else:
                fit[m["serial"]][n["serial"]] = score(m, n)
    ms = [m["serial"] for m in missing if fit[m["serial"]]]
    best = _best(ms, fit)
    out = []
    for m in missing:
        sd = m["serial"]
        if not fit[sd]:
            continue
        got = {b.get(sd) for b in best} if best else set()
        pick = next(iter(got)) if len(got) == 1 and None not in got else ""
        order = sorted(fit[sd], key=lambda n: (n != pick, -fit[sd][n], n))
        users = {n: sum(1 for x in fit.values() if n in x) for n in fit[sd]}
        out.append({"missing": sd, "choices": order, "pick": pick,
                    "only": len(fit[sd]) == 1 and users[order[0]] == 1, "why": why[sd]})
    return out


def _best(ms: list[str], fit: dict[str, dict[str, int]]) -> list[dict[str, str | None]]:
    """Every assignment of new dongles to the missing ones `ms` (one each, at most) with the highest (count, score)."""
    if not ms:
        return []
    if len(ms) > MAX_PAIRING:
        taken: set[str] = set()
        one: dict[str, str | None] = {}
        for m in sorted(ms, key=lambda m: -max(fit[m].values())):
            n = max((n for n in fit[m] if n not in taken), key=lambda n: (fit[m][n], n), default=None)
            one[m] = n
            if n:
                taken.add(n)
        return [one]
    best: list[dict[str, str | None]] = []
    top = (-1, -1)

    def walk(k: int, taken: dict[str, str | None], count: int, total: int) -> None:
        nonlocal best, top
        if k == len(ms):
            if (count, total) > top:
                top, best = (count, total), [dict(taken)]
            elif (count, total) == top:
                best.append(dict(taken))
            return
        m = ms[k]
        used = set(taken.values())
        for n, s in fit[m].items():
            if n not in used:
                taken[m] = n
                walk(k + 1, taken, count + 1, total + s)
        taken[m] = None
        walk(k + 1, taken, count, total)
        del taken[m]

    walk(0, {}, 0, 0)
    return best
