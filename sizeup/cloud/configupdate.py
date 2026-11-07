"""What an update adds to a station's own config (GitHub #119): /etc/sizeup/config.toml is made from
deploy/station/config.station.toml once and never overwritten, so a channel the template gains later
(FG2) would never reach a station that is already installed. Each addition here runs once per station,
at the switch to a release that has it (deploy/station/install-linux.sh activate, as root):

    fg2      the county's Fireground 2 (458.0375 MHz NFM, PL 100; county fireground list 2024-11-06,
             docs/PLAN.md) as a traffic channel like FG1, appended after the config's channels; and
             "fg2" at the end of [radio] auto_assign_order when that list is there (else the code's
             default order already ends with it), so only a 4th free dongle takes it by itself.
             Skipped: a config with no [[radio.channels]] (no radios), or one that already has a
             channel called FG2 or on 458.0375.

    tones-47 our two-tone codes as heard on the air (GitHub #47: 60 Control tone tests 10-20 and
             10-23-2026): the [[tones]] entry on the RadioReference wiki's unverified Port Chester pair
             349.1/311.1 (the template's until then) gets 903.2/810.2, and Rye Brook FD 767.4/928.1
             (ours) is added after it. The station's [[tones]] are in its tone plan: the live paging
             display (its "ours" goes to the hub as it is) and the station page. Both names are ours
             whatever the file says (sizeup/config.py) and the release's list has both heard pairs, but
             the wiki pair left there stays a Port Chester pair on the station, and nowhere else.
             Only when an entry is exactly on 349.1/311.1: any other [[tones]] (set by hand, or a new
             install's) are left as they are. Rye Brook is added only with that replacement, and not
             when an entry is already on 767.4/928.1 or called Rye Brook FD.

    hpn-atc  HPN ATC (GitHub #233): Westchester Tower 118.575 MHz AM as an airband channel ("HPN Tower"), appended
             after the config's channels, and "hpn tower" at the end of [radio] auto_assign_order when that list is
             there, so only a 5th free dongle takes it by itself (pick it by hand for the airband dongle). Skipped: no
             [[radio.channels]], or a channel already called HPN Tower, on 118.575, or of kind "airband" (but for
             approach-ground's own three, which a station may have first: they are not a tower).
             Rollback-safe: a release from before #233 refuses kind = "airband" (and an order naming a channel it
             doesn't have), so a switch back to it would leave the station down. So it is only applied when the
             release being left (--leaving, the one a failed switch goes back to) reads airband channels; else it
             waits ("deferred", not marked) for the next update, whose release to go back to is this one.
    adsb-receiver  the [aircraft.receiver] table for our own ADS-B receiver (GitHub #235): sdr = "" (no dongle
             named: nothing runs), gain = "auto", bias_tee = false, with the comments that say what each does, so
             naming the dongle (here or in Settings > Radios) and choosing bias-tee for its LNA is one line each.
             Skipped: a config that already has an [aircraft.receiver] (or aircraft.receiver keys). Rollback-safe like
             hpn-atc: a release from before #235 refuses gain / bias_tee, so it waits ("deferred") until the
             release being left reads them.

    approach-ground  NY Approach and HPN Ground (GitHub #260): three airband channels appended after the config's
             channels, choices for a dongle in Settings > Radios, NEVER added to [radio] auto_assign_order (so no
             dongle takes one by itself; pick it by hand): "NY Approach" (New York Approach for HPN, 126.4 MHz
             0700-2300 local and 120.8 2300-0700, a schedule the receiver retunes on), "NY Approach 124.65" (the
             Chart Supplement's third Approach frequency, fixed) and "HPN Ground" (Westchester Ground, 121.825).
             Each one is skipped when the config has it already: a channel by that label, or for the fixed ones on
             that frequency. Skipped: no [[radio.channels]]. Rollback-safe like hpn-atc: a release from before #260
             refuses the schedule key (and one from before #233 kind = "airband"), so it is only applied when the
             release being left reads "schedule"; else it waits ("deferred", not marked) for the next update.

    south-end  the station's name (GitHub #271): the station PC is at the South End firehouse (51-53 Grace Church St),
             not Headquarters, so [station] name "Port Chester FD Headquarters" (the template's until then) becomes
             "Port Chester FD South End". Only when the name is exactly the old default: one set by hand is left as
             it is. The id (pcfd-hq) never changes: the hub keys the station's token, history and outbox on it. Not
             deferred: every release reads [station] name, so a switch back is safe with either name.

    airband-sets  clearer names and several frequencies per dongle (GitHub #275): "NY Approach" becomes "NY App 126.4/120.8"
             and "NY Approach 124.65" "NY App 124.65" (the label line only: a dongle's saved job follows, sizeup/app.py
             RENAMED); NY App 126.4/120.8 gets also = 124.65 07:00-23:00 (126.4 + 124.65 on one dongle by day, only
             with [radio] airband_multichannel on, which this doesn't set); HPN Ground gets a
             schedule, 121.825 06:00-23:00 and 124.65 23:00-06:00 (Ground is closed overnight: its dongle hears
             Approach's 124.65 then). Each part only on a channel exactly as approach-ground / hpn-atc / the template
             left it (that label, frequency and schedule, no "also" yet) and only when the frequency it adds has a
             channel here (its transmissions are that channel's); the rest is left as it is. Rollback-safe like
             approach-ground: a release from before #275 refuses "also", so it waits ("deferred") until the release
             being left reads it. And it waits for approach-ground (marked done): never marked done before the
             channels it changes are there.

Done ones are listed in a marks file (/etc/sizeup/config-updates, one name per line), so one taken out
by hand stays out. The config is only ever added to (tones-47 also changes the two values of the one
entry it names): the text is kept as it is, the result must parse
(tomllib) and say what was meant, else nothing is written. The old file is kept beside it
(config.toml.before-<name>), the new one replaces it with one rename and the same owner and mode.

Run by the system python3 (stdlib only, like autoupdate.py), from the release being switched to:

    python3 -I configupdate.py /etc/sizeup/config.toml /etc/sizeup/config-updates [--leaving /opt/sizeup/releases/<sha>]

One line per change or skip on stdout; exit 0 unless something couldn't be read or written.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

FG2_MHZ = 458.0375
FG2_BLOCK = """
# Added by the update to FG2 (GitHub #119): the county's Fireground 2, PCFD / Rye Brook secondary
# (county fireground list 2024-11-06), received like FG1. Pick it for a dongle on the station page
# (Radios) or the hub's; only a 4th free dongle takes it by itself.
[[radio.channels]]
label = "FG2"
mhz = 458.0375
kind = "traffic"
device = "fire-fg2"
gain_db = 40
"""
ORDER_LINE = re.compile(r"^(?P<head>[ \t]*auto_assign_order[ \t]*=[ \t]*\[)(?P<items>[^\]\n#]*)(?P<tail>\])", re.M)


def _key(name) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def add_fg2(text: str) -> tuple[str, str]:
    """(new text, what was done). The text unchanged when there's nothing to do (the reason says why).
    ValueError when the config doesn't parse or the change can't be made safely."""
    data = tomllib.loads(text)
    radio = data.get("radio") if isinstance(data.get("radio"), dict) else {}
    chans = radio.get("channels") if isinstance(radio.get("channels"), list) else []
    if not chans:
        return text, "fg2: no [[radio.channels]] here (no radios): nothing added"
    for c in chans:
        if isinstance(c, dict) and (_key(c.get("label", "")) == "fg2" or _mhz(c.get("mhz")) == FG2_MHZ):
            return text, f"fg2: already has {c.get('label')!r} on {c.get('mhz')} MHz: nothing added"
    order = radio.get("auto_assign_order")
    new = text if text.endswith("\n") else text + "\n"
    did = ["FG2 (458.0375 MHz, traffic) added after the channels"]
    if isinstance(order, list) and order and "fg2" not in {_key(x) for x in order}:
        found = list(ORDER_LINE.finditer(new))
        if len(found) != 1:
            raise ValueError("[radio] auto_assign_order isn't on one line: add \"fg2\" to it by hand")
        m = found[0]
        items = m.group("items").rstrip()
        sep = "" if items.rstrip().endswith(",") else ","
        new = new[:m.start("items")] + f'{items}{sep} "fg2"' + new[m.end("items"):]
        did.append('"fg2" at the end of auto_assign_order')
    new += FG2_BLOCK
    _check(new, order)
    return new, "fg2: " + "; ".join(did)


HPN_MHZ = 118.575
HPN_BLOCK = """
# Added by the update to HPN ATC (GitHub #233): Westchester County Airport tower, 118.575 MHz AM (FAA Chart
# Supplement via airnav.com/airport/KHPN). Its transcripts feed the airport pre-alerts. Pick it for the airband
# dongle on the station page (Radios) or the hub's; only a 5th free dongle takes it by itself.
[[radio.channels]]
label = "HPN Tower"
mhz = 118.575
kind = "airband"
device = "hpn-twr"
gain_db = 36.4
squelch_db = 3.5
"""


class Deferred(Exception):
    """Not now (not marked): tried again at the next update."""


def reads_airband(release: Path | None) -> bool:
    """Whether the code of that release accepts kind = "airband" (None: no release to go back to)."""
    if release is None:
        return True
    try:
        return '"airband"' in (release / "sizeup" / "config.py").read_text(errors="replace")
    except OSError:
        return False


def add_hpn_atc(text: str, leaving: Path | None = None) -> tuple[str, str]:
    """(new text, what was done): the HPN Tower airband channel and "hpn tower" last in auto_assign_order. Deferred
    while the release being left can't read it; ValueError when the config doesn't parse or it can't be done safely."""
    data = tomllib.loads(text)
    radio = data.get("radio") if isinstance(data.get("radio"), dict) else {}
    chans = radio.get("channels") if isinstance(radio.get("channels"), list) else []
    if not chans:
        return text, "hpn-atc: no [[radio.channels]] here (no radios): nothing added"
    later = {_key(label) for label, _m, _b in APPROACH_GROUND}     # approach-ground's (#260) aren't a tower ...
    later |= {_key(label) for label in RENAMES.values()}            # ... nor are they by their #275 names
    for c in chans:
        if isinstance(c, dict) and (_key(c.get("label", "")) == "hpntower" or _mhz(c.get("mhz")) == HPN_MHZ
                                    or (c.get("kind") == "airband" and _key(c.get("label", "")) not in later)):
            return text, f"hpn-atc: already has {c.get('label')!r} on {c.get('mhz')} MHz: nothing added"
    if not reads_airband(leaving):
        raise Deferred(f"the release this switch would go back to ({leaving.name}) can't read an airband channel: "
                       "HPN Tower is added at the next update")
    order = radio.get("auto_assign_order")
    new = text if text.endswith("\n") else text + "\n"
    did = ["HPN Tower (118.575 MHz, airband AM) added after the channels"]
    if isinstance(order, list) and order and "hpntower" not in {_key(x) for x in order}:
        found = list(ORDER_LINE.finditer(new))
        if len(found) != 1:
            raise ValueError('[radio] auto_assign_order is not on one line: add "hpn tower" to it by hand')
        m = found[0]
        items = m.group("items").rstrip()
        sep = "" if items.rstrip().endswith(",") else ","
        new = new[:m.start("items")] + f'{items}{sep} "hpn tower"' + new[m.end("items"):]
        did.append('"hpn tower" at the end of auto_assign_order')
    new += HPN_BLOCK
    try:
        d = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result wouldn't parse ({e}): nothing changed") from None
    r = d.get("radio") or {}
    last = (r.get("channels") or [{}])[-1]
    if (last.get("label"), last.get("mhz"), last.get("kind")) != ("HPN Tower", HPN_MHZ, "airband"):
        raise ValueError("the result doesn't end with the HPN Tower channel: nothing changed")
    if isinstance(order, list) and order:
        got = r.get("auto_assign_order")
        want = [*(_key(x) for x in order), *([] if "hpntower" in {_key(x) for x in order} else ["hpntower"])]
        if not isinstance(got, list) or [_key(x) for x in got] != want:
            raise ValueError("auto_assign_order came out wrong: nothing changed")
    if {k: v for k, v in d.items() if k != "radio"} != {k: v for k, v in data.items() if k != "radio"}:
        raise ValueError("the result doesn't say what was meant: nothing changed")
    return new, "hpn-atc: " + "; ".join(did)


def _mhz(v) -> float | None:
    try:
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def _check(new: str, order) -> None:
    """The result says what was meant: one FG2 traffic channel at the end, the order (if any) ending in fg2."""
    try:
        d = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result wouldn't parse ({e}): nothing changed") from None
    r = d.get("radio") or {}
    last = (r.get("channels") or [{}])[-1]
    if (last.get("label"), last.get("mhz"), last.get("kind")) != ("FG2", FG2_MHZ, "traffic"):
        raise ValueError("the result doesn't end with the FG2 channel: nothing changed")
    if isinstance(order, list) and order:
        got = r.get("auto_assign_order")
        if not isinstance(got, list) or [_key(x) for x in got] != [*(_key(x) for x in order), "fg2"]:
            raise ValueError("auto_assign_order came out wrong: nothing changed")


OLD_PC = (349.1, 311.1)              # RadioReference wiki (2025-07), never heard in 18 days of pages (#47)
PC = (903.2, 810.2)                  # Port Chester Fire, heard on air (#47)
RB = (767.4, 928.1)                  # Rye Brook Fire, heard on air (#47)
HEARD = "# heard on air, 60 Control tone tests 10-20 and 10-23-2026 (#47)"
RB_BLOCK = f"""
# Added by the update to the heard tones (GitHub #47): Rye Brook Fire, ours (PCFD answers both villages;
# 60 Control often stacks the two pairs on one page, "Rye Brook with Port Chester ...").
[[tones]]
name = "Rye Brook FD"
a_hz = 767.4                       {HEARD}
b_hz = 928.1
agency = "RBFD"
ours = true
"""
HEADER = re.compile(r"^[ \t]*\[\[?[ \t]*[A-Za-z0-9_.\-\"' ]+?[ \t]*\]\]?[ \t]*(?:#.*)?$", re.M)
TONES_HEADER = re.compile(r"^[ \t]*\[\[[ \t]*tones[ \t]*\]\][ \t]*(?:#.*)?$")
KEY_LINE = re.compile(r"^[ \t]*[A-Za-z0-9_\-\"']+[ \t]*=", re.M)


def _pair(t, exact: bool = False) -> tuple[float | None, float | None]:
    """An entry's (A, B): as written (exact), else to 0.1 Hz."""
    if not isinstance(t, dict):
        return None, None
    return _hz(t.get("a_hz"), exact), _hz(t.get("b_hz"), exact)


def _hz(v, exact: bool = False) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if exact else round(float(v), 1)


def _value_line(seg: str, key: str, old: float, new: float, note: str) -> str:
    """seg with `key = old` (one line, any comment) as `key = new` and the note; ValueError unless exactly one."""
    rx = re.compile(rf"^(?P<head>[ \t]*{key}[ \t]*=[ \t]*){re.escape(str(old))}[ \t]*(?:#[^\n]*)?$", re.M)
    found = list(rx.finditer(seg))
    if len(found) != 1:
        raise ValueError(f"the [[tones]] entry's {key} isn't one plain line: change it to {new} by hand")
    m = found[0]
    line = f"{m.group('head')}{new}"
    if note:
        line = line.ljust(35) + note if len(line) < 35 else f"{line}  {note}"
    return seg[:m.start()] + line + seg[m.end():]


def fix_tones(text: str) -> tuple[str, str]:
    """(new text, what was done): the unverified Port Chester pair 349.1/311.1 -> 903.2/810.2 as heard, and Rye
    Brook FD 767.4/928.1 (ours) after it. The text unchanged when there's nothing to do (the reason says why).
    ValueError when the config doesn't parse or the change can't be made safely."""
    data = tomllib.loads(text)
    tones = data.get("tones") if isinstance(data.get("tones"), list) else []
    old = [i for i, t in enumerate(tones) if _pair(t, exact=True) == OLD_PC]
    if not old:
        return text, "tones-47: no [[tones]] entry on the unverified 349.1/311.1: nothing changed"
    if len(old) > 1:
        raise ValueError("two [[tones]] entries on 349.1/311.1: fix them by hand")
    if any(_pair(t) == PC for t in tones):
        return text, "tones-47: has 903.2/810.2 next to 349.1/311.1 (set by hand?): nothing changed"
    heads = list(HEADER.finditer(text))
    segs = []                                    # (start, end) of each [[tones]] table, header line included
    for i, h in enumerate(heads):
        if TONES_HEADER.match(h.group(0)):
            segs.append((h.start(), heads[i + 1].start() if i + 1 < len(heads) else len(text)))
    if len(segs) != len(tones):
        raise ValueError("the [[tones]] tables aren't all plain [[tones]] headers: change them by hand")
    s, e = segs[old[0]]
    seg = _value_line(text[s:e], "a_hz", OLD_PC[0], PC[0], HEARD)
    seg = _value_line(seg, "b_hz", OLD_PC[1], PC[1], "# (was 349.1/311.1, RadioReference wiki, never heard)")
    did = ["Port Chester's 349.1/311.1 is now 903.2/810.2"]
    has_rb = any(_pair(t) == RB or _key(t.get("name", "")) == "ryebrookfd" for t in tones if isinstance(t, dict))
    if has_rb:
        did.append("Rye Brook was already there")
    else:
        keys = list(KEY_LINE.finditer(seg))
        cut = seg.index("\n", keys[-1].start()) + 1 if keys and "\n" in seg[keys[-1].start():] else len(seg)
        head = seg[:cut] if seg[:cut].endswith("\n") else seg[:cut] + "\n"
        seg = head + RB_BLOCK + seg[cut:]
        did.append("Rye Brook FD 767.4/928.1 (ours) added after it")
    new = text[:s] + seg + text[e:]
    want = [dict(t) for t in tones]
    want[old[0]].update(a_hz=PC[0], b_hz=PC[1])
    if not has_rb:
        want.insert(old[0] + 1, {"name": "Rye Brook FD", "a_hz": RB[0], "b_hz": RB[1], "agency": "RBFD", "ours": True})
    try:
        got = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result wouldn't parse ({e}): nothing changed") from None
    if got.get("tones") != want or {k: v for k, v in got.items() if k != "tones"} != \
            {k: v for k, v in data.items() if k != "tones"}:
        raise ValueError("the result doesn't say what was meant: nothing changed")
    return new, "tones-47: " + "; ".join(did)


ADSB_BLOCK = """
# Added by the update to our own ADS-B receiver (GitHub #235, deploy/station/README.md "ADS-B receiver"): the dongle
# behind the 1090 MHz filter and LNA, by its exact USB serial, never all digits (rtl_eeprom -d <index> -s adsb1090).
# Named here or in Settings > Radios (admins, with a confirm; that choice wins and is kept in state/): the decoder
# (sizeup-adsb.service, readsb) runs on that dongle and only on it, sizeup's radios and auto-assign never take it,
# and this station sends what it hears to the hub every second or two. "" = no ADS-B receiver: nothing runs.
[aircraft.receiver]
sdr = ""
gain = "auto"                      # readsb --gain: auto (an LNA in front makes a fixed high gain overload), or dB
bias_tee = false                   # true ONLY for an LNA powered up the coax (4.5 V); false for its own supply or none
"""
ADSB_WANT = {"sdr": "", "gain": "auto", "bias_tee": False}


def reads_bias_tee(release: Path | None) -> bool:
    """Whether the code of that release accepts [aircraft.receiver] gain / bias_tee (None: no release to go back to)."""
    if release is None:
        return True
    try:
        return '"bias_tee"' in (release / "sizeup" / "config.py").read_text(errors="replace")
    except OSError:
        return False


def add_adsb(text: str, leaving: Path | None = None) -> tuple[str, str]:
    """(new text, what was done): an [aircraft.receiver] table with nothing named, at the end. The text unchanged when
    the config has one already. ValueError when the config doesn't parse or the result wouldn't say what was meant;
    Deferred while the release a failed switch would go back to can't read it."""
    data = tomllib.loads(text)
    ac = data.get("aircraft")
    if isinstance(ac, dict) and "receiver" in ac:
        return text, "adsb-receiver: already has [aircraft.receiver]: nothing added"
    if not reads_bias_tee(leaving):
        raise Deferred(f"the release this switch would go back to ({leaving.name}) can't read [aircraft.receiver] "
                       "gain / bias_tee: added at the next update")
    new = (text if text.endswith("\n") else text + "\n") + ADSB_BLOCK
    try:
        got = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result wouldn't parse ({e}): nothing changed") from None
    rest = {k: v for k, v in got.items() if k != "aircraft"}
    gac = dict(got.get("aircraft") or {})
    if gac.pop("receiver", None) != ADSB_WANT or rest != {k: v for k, v in data.items() if k != "aircraft"} \
            or gac != (ac if isinstance(ac, dict) else {}):
        raise ValueError("the result doesn't say what was meant: nothing changed")
    return new, 'adsb-receiver: [aircraft.receiver] added (sdr = "": no ADS-B dongle named yet; bias_tee = false)'


# NY Approach and HPN Ground (#260), FAA Chart Supplement via airnav.com/airport/KHPN (FAA information effective
# 29 October 2026): NEW YORK APPROACH 120.8 2300-0700, 126.4 0700-2300, 124.65; WESTCHESTER GROUND 121.825 (0600-2300).
APPROACH_GROUND = (
    ("NY Approach", (126.4, 120.8), """
# Added by the update to NY Approach and HPN Ground (GitHub #260): New York Approach for HPN (FAA Chart Supplement via
# airnav.com/airport/KHPN): 126.4 MHz 0700-2300, 120.8 2300-0700 local time; the receiver retunes on the schedule,
# between transmissions. Pick it for a dongle on the station page (Radios) or the hub's: it is never auto-assigned.
[[radio.channels]]
label = "NY Approach"
mhz = 126.4
kind = "airband"
schedule = [{ at = "07:00", mhz = 126.4 }, { at = "23:00", mhz = 120.8 }]   # local time (America/New_York)
device = "nyapp"
gain_db = 36.4
squelch_db = 3.5
"""),
    ("NY Approach 124.65", (124.65,), """
# Added by the update to NY Approach and HPN Ground (GitHub #260): New York Approach's third HPN frequency, 124.65 MHz
# (FAA Chart Supplement), fixed. Picked by hand, never auto-assigned.
[[radio.channels]]
label = "NY Approach 124.65"
mhz = 124.65
kind = "airband"
device = "nyapp2"
gain_db = 36.4
squelch_db = 3.5
"""),
    ("HPN Ground", (121.825,), """
# Added by the update to NY Approach and HPN Ground (GitHub #260): Westchester Ground, 121.825 MHz (FAA Chart
# Supplement; 0600-2300 like the tower). Picked by hand, never auto-assigned.
[[radio.channels]]
label = "HPN Ground"
mhz = 121.825
kind = "airband"
device = "hpn-gnd"
gain_db = 36.4
squelch_db = 3.5
"""),
)
APPROACH_SCHEDULE = [{"at": "07:00", "mhz": 126.4}, {"at": "23:00", "mhz": 120.8}]


def reads_schedule(release: Path | None) -> bool:
    """Whether the code of that release accepts an airband channel's schedule (#260; None: no release to go back to)."""
    if release is None:
        return True
    try:
        return '"schedule"' in (release / "sizeup" / "config.py").read_text(errors="replace")
    except OSError:
        return False


def add_approach_ground(text: str, leaving: Path | None = None) -> tuple[str, str]:
    """(new text, what was done): NY Approach (scheduled), NY Approach 124.65 and HPN Ground, the ones not there yet, after
    the channels; auto_assign_order untouched. Deferred while the release being left can't read a schedule; ValueError
    when the config doesn't parse or the result wouldn't say what was meant."""
    data = tomllib.loads(text)
    radio = data.get("radio") if isinstance(data.get("radio"), dict) else {}
    chans = radio.get("channels") if isinstance(radio.get("channels"), list) else []
    if not chans:
        return text, "approach-ground: no [[radio.channels]] here (no radios): nothing added"
    have_labels = {_key(c.get("label", "")) for c in chans if isinstance(c, dict)}
    have_mhz = {_mhz(c.get("mhz")) for c in chans if isinstance(c, dict)}
    todo, there = [], []
    for label, mhzs, block in APPROACH_GROUND:
        fixed = len(mhzs) == 1
        if _key(label) in have_labels or (fixed and _mhz(mhzs[0]) in have_mhz) \
                or _key(RENAMES.get(label, label)) in have_labels:          # there by its #275 name
            there.append(label)
        else:
            todo.append((label, mhzs, block))
    if not todo:
        return text, f"approach-ground: already has {', '.join(there)}: nothing added"
    if not reads_schedule(leaving):
        raise Deferred(f"the release this switch would go back to ({leaving.name}) can't read an airband channel's "
                       "schedule: NY Approach and HPN Ground are added at the next update")
    new = text if text.endswith("\n") else text + "\n"
    for _label, _mhzs, block in todo:
        new += block
    try:
        d = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result wouldn't parse ({e}): nothing changed") from None
    r = d.get("radio") or {}
    got = (r.get("channels") or [])
    if got[:len(chans)] != chans or len(got) != len(chans) + len(todo):
        raise ValueError("the channels before came out changed: nothing changed")
    for c, (label, mhzs, _block) in zip(got[len(chans):], todo):
        if (c.get("label"), c.get("mhz"), c.get("kind")) != (label, mhzs[0], "airband") \
                or c.get("schedule", None) != (APPROACH_SCHEDULE if len(mhzs) > 1 else None):
            raise ValueError(f"the {label} channel came out wrong: nothing changed")
    if {k: v for k, v in r.items() if k != "channels"} != {k: v for k, v in radio.items() if k != "channels"} \
            or {k: v for k, v in d.items() if k != "radio"} != {k: v for k, v in data.items() if k != "radio"}:
        raise ValueError("the result doesn't say what was meant: nothing changed")
    did = [f"{label} ({'126.4 MHz 0700-2300, 120.8 2300-0700' if len(mhzs) > 1 else f'{mhzs[0]:g} MHz'}, airband AM)"
           for label, mhzs, _block in todo]
    return new, ("approach-ground: " + ", ".join(did) + " added after the channels, picked by hand (never auto-assigned)"
                 + (f"; already there: {', '.join(there)}" if there else ""))


OLD_NAME = "Port Chester FD Headquarters"
SOUTH_END = "Port Chester FD South End"
STATION_HEADER = re.compile(r"^[ \t]*\[[ \t]*station[ \t]*\][ \t]*(?:#.*)?$", re.M)
NAME_LINE = re.compile(r'^(?P<head>[ \t]*name[ \t]*=[ \t]*)"Port Chester FD Headquarters"(?P<tail>[ \t]*(?:#[^\n]*)?)$', re.M)


def rename_south_end(text: str) -> tuple[str, str]:
    """(new text, what was done): [station] name "Port Chester FD Headquarters" -> "Port Chester FD South End". The
    text unchanged when the name is anything else (the reason says why). ValueError when the config doesn't parse or
    the change can't be made safely."""
    data = tomllib.loads(text)
    st = data.get("station") if isinstance(data.get("station"), dict) else {}
    name = st.get("name")
    if name == SOUTH_END:
        return text, f"south-end: the station is already called {SOUTH_END!r}: nothing changed"
    if name != OLD_NAME:
        return text, f"south-end: the station is called {name!r}, not the old default: nothing changed"
    heads = list(HEADER.finditer(text))
    top = [h for h in heads if STATION_HEADER.match(h.group(0))]
    if len(top) != 1:
        raise ValueError("[station] isn't one plain [station] header: change its name by hand")
    i = heads.index(top[0])
    s, e = top[0].end(), heads[i + 1].start() if i + 1 < len(heads) else len(text)
    found = list(NAME_LINE.finditer(text, s, e))
    if len(found) != 1:
        raise ValueError(f'[station] name isn\'t one plain line: change it to "{SOUTH_END}" by hand')
    m = found[0]
    new = text[:m.start()] + f'{m.group("head")}"{SOUTH_END}"{m.group("tail")}' + text[m.end():]
    try:
        got = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result wouldn't parse ({e}): nothing changed") from None
    if got != {**data, "station": {**st, "name": SOUTH_END}}:
        raise ValueError("the result doesn't say what was meant: nothing changed")
    return new, f'south-end: [station] name is now "{SOUTH_END}" (was "{OLD_NAME}"; the id {st.get("id")!r} is kept)'


# Several frequencies per dongle and clearer names (#275). FAA Chart Supplement via airnav.com/airport/KHPN (FAA
# information effective 29 October 2026): WESTCHESTER TOWER 118.575 [0600-2300] (CTAF 118.575), WESTCHESTER GROUND
# 121.825 [0600-2300], NEW YORK APPROACH 120.8 2300-0700, 126.4 0700-2300, 124.65.
RENAMES = {"NY Approach": "NY App 126.4/120.8", "NY Approach 124.65": "NY App 124.65"}
APPROACH_ALSO = [{"mhz": 124.65, "from": "07:00", "until": "23:00"}]
GROUND_SCHEDULE = [{"at": "06:00", "mhz": 121.825}, {"at": "23:00", "mhz": 124.65}]
CHANNELS_HEADER = re.compile(r"^[ \t]*\[\[[ \t]*radio\.channels[ \t]*\]\][ \t]*(?:#.*)?$")
LABEL_LINE = r'^(?P<head>[ \t]*label[ \t]*=[ \t]*){old}(?P<tail>[ \t]*(?:#[^\n]*)?)$'


def reads_also(release: Path | None) -> bool:
    """Whether the code of that release accepts an airband channel's "also" (#275; None: no release to go back to)."""
    if release is None:
        return True
    try:
        return '"also"' in (release / "sizeup" / "config.py").read_text(errors="replace")
    except OSError:
        return False


def _toml_list(entries: list[dict]) -> str:
    def val(v):
        return f'"{v}"' if isinstance(v, str) else f"{v:g}"
    return "[" + ", ".join("{ " + ", ".join(f"{k} = {val(v)}" for k, v in e.items()) + " }" for e in entries) + "]"


def _add_line(seg: str, line: str) -> str:
    """seg (one [[radio.channels]] table) with line after its last key line."""
    keys = list(KEY_LINE.finditer(seg))
    if not keys:
        raise ValueError("a [[radio.channels]] table with no keys: change it by hand")
    end = seg.find("\n", keys[-1].start())
    cut = len(seg) if end < 0 else end + 1
    head = seg[:cut] if seg[:cut].endswith("\n") else seg[:cut] + "\n"
    return head + line + "\n" + seg[cut:]


def airband_sets(text: str, leaving: Path | None = None) -> tuple[str, str]:
    """(new text, what was done): the #275 names, Approach's "also" and Ground's schedule, each on a
    channel exactly as it was added. Deferred while the release being left can't read "also"; ValueError when the
    config doesn't parse or the result wouldn't say what was meant."""
    data = tomllib.loads(text)
    radio = data.get("radio") if isinstance(data.get("radio"), dict) else {}
    chans = radio.get("channels") if isinstance(radio.get("channels"), list) else []
    if not chans:
        return text, "airband-sets: no [[radio.channels]] here (no radios): nothing changed"
    labels = {c.get("label") for c in chans if isinstance(c, dict)}

    def has(mhz: float) -> bool:                  # a channel the frequency belongs to (its mhz, or its schedule)
        return any(isinstance(c, dict) and c.get("kind") == "airband"
                   and (_mhz(c.get("mhz")) == mhz or any(isinstance(e, dict) and _mhz(e.get("mhz")) == mhz
                                                        for e in (c.get("schedule") or [])))
                   for c in chans)

    plan: dict[int, tuple[str | None, str | None]] = {}      # channel index -> (new label, line to add)
    did: dict[int, list[str]] = {}
    for i, c in enumerate(chans):
        if not isinstance(c, dict) or c.get("kind") != "airband" or "also" in c:
            continue
        label, mhz = c.get("label"), _mhz(c.get("mhz"))
        if label == "NY Approach" and mhz == 126.4 and c.get("schedule") == APPROACH_SCHEDULE:
            new = RENAMES[label] if RENAMES[label] not in labels else None
            line = f"also = {_toml_list(APPROACH_ALSO)}   # #275: 124.65 too, by day (one dongle, 2.048 MS/s)" \
                if has(124.65) else None
            plan[i] = (new, line)
        elif label == "NY Approach 124.65" and mhz == 124.65 and "schedule" not in c:
            if RENAMES[label] not in labels:
                plan[i] = (RENAMES[label], None)
        elif label == "HPN Ground" and mhz == 121.825 and "schedule" not in c and has(124.65):
            plan[i] = (None, f"schedule = {_toml_list(GROUND_SCHEDULE)}   # #275: Ground closed 23:00-06:00: 124.65 then")
    plan = {i: p for i, p in plan.items() if p != (None, None)}
    if not plan:
        return text, "airband-sets: no channel as it was added (NY Approach, HPN Ground): nothing changed"
    if not reads_also(leaving):
        raise Deferred(f"the release this switch would go back to ({leaving.name}) can't read an airband channel's "
                       '"also": the #275 names and frequencies are changed at the next update')
    heads = list(HEADER.finditer(text))
    segs = []
    for k, h in enumerate(heads):
        if CHANNELS_HEADER.match(h.group(0)):
            segs.append((h.start(), heads[k + 1].start() if k + 1 < len(heads) else len(text)))
    if len(segs) != len(chans):
        raise ValueError("the [[radio.channels]] tables aren't all plain [[radio.channels]] headers: change them by hand")
    want = [dict(c) for c in chans]
    new_text = text
    for i in sorted(plan, reverse=True):            # from the end, so the earlier offsets hold
        s, e = segs[i]
        seg = new_text[s:e]
        label, line = plan[i]
        old = chans[i]["label"]
        if label:
            rx = re.compile(LABEL_LINE.format(old=re.escape(f'"{old}"')), re.M)
            found = list(rx.finditer(seg))
            if len(found) != 1:
                raise ValueError(f'the {old} channel\'s label isn\'t one plain line: rename it to "{label}" by hand')
            m = found[0]
            seg = seg[:m.start()] + f'{m.group("head")}"{label}"{m.group("tail")}' + seg[m.end():]
            want[i]["label"] = label
            did.setdefault(i, []).append(f'"{old}" is now "{label}"')
        if line:
            seg = _add_line(seg, line)
            key = line.split("=", 1)[0].strip()
            want[i][key] = tomllib.loads(line.split("#", 1)[0])[key]
            did.setdefault(i, []).append(f"{label or old}: {key} " + ("124.65 07:00-23:00" if key == "also" else
                                                                     "121.825 06:00-23:00, 124.65 23:00-06:00"))
        new_text = new_text[:s] + seg + new_text[e:]
    if not new_text.endswith("\n") and text.endswith("\n"):
        new_text += "\n"
    try:
        got = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"the result wouldn't parse ({e}): nothing changed") from None
    r = got.get("radio") or {}
    if r.get("channels") != want or {k: v for k, v in r.items() if k != "channels"} != \
            {k: v for k, v in radio.items() if k != "channels"} or \
            {k: v for k, v in got.items() if k != "radio"} != {k: v for k, v in data.items() if k != "radio"}:
        raise ValueError("the result doesn't say what was meant: nothing changed")
    return new_text, "airband-sets: " + "; ".join(x for i in sorted(did) for x in did[i])


UPDATES = (("fg2", add_fg2), ("tones-47", fix_tones), ("hpn-atc", add_hpn_atc), ("adsb-receiver", add_adsb),
           ("approach-ground", add_approach_ground), ("south-end", rename_south_end), ("airband-sets", airband_sets))
NEEDS_LEAVING = {"hpn-atc", "adsb-receiver", "approach-ground", "airband-sets"}   # given the release being left
AFTER = {"airband-sets": "approach-ground"}       # not before that one is done (marked): what it changes comes from it


def run(conf: Path, marks: Path, leaving: Path | None = None) -> list[str]:
    """Every update not in marks, applied to conf in turn. Lines to say. leaving: the release the switch leaves
    (a failed switch goes back to it), for the updates an older release couldn't read."""
    done = set(marks.read_text().split()) if marks.exists() else set()
    todo = [(n, f) for n, f in UPDATES if n not in done]
    if not todo:
        return []
    text = conf.read_text()
    out = []
    for name, fn in todo:
        if AFTER.get(name) and AFTER[name] not in done:
            out.append(f"{name}: deferred: waits for {AFTER[name]} (not done yet)")
            continue                                  # not marked: tried again at the next update
        try:
            new, said = fn(text, leaving) if name in NEEDS_LEAVING else fn(text)
        except Deferred as e:
            out.append(f"{name}: deferred: {e}")
            continue                                  # not marked: tried again at the next update
        except (ValueError, tomllib.TOMLDecodeError) as e:
            out.append(f"{name}: not applied: {e}")
            continue                                  # not marked: tried again at the next update
        if new != text:
            st = conf.stat()
            keep = conf.with_name(f"{conf.name}.before-{name}")
            keep.write_text(text)
            os.chmod(keep, st.st_mode & 0o777)
            tmp = conf.with_name(f".{conf.name}.{os.getpid()}")
            tmp.write_text(new)
            os.chmod(tmp, st.st_mode & 0o777)
            if os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
                os.chown(keep, st.st_uid, st.st_gid)
            os.replace(tmp, conf)
            text = new
            said += f" (the old file: {keep})"
        out.append(said)
        done.add(name)
        marks.write_text("".join(f"{n}\n" for n, _f in UPDATES if n in done))
    return out


def main(argv: list[str]) -> int:
    leaving = None
    if len(argv) == 4 and argv[2] == "--leaving":
        leaving = Path(argv[3]) if argv[3] else None
        argv = argv[:2]
    if len(argv) != 2:
        print("usage: configupdate.py CONFIG MARKS [--leaving RELEASE_DIR]", file=sys.stderr)
        return 2
    conf, marks = Path(argv[0]), Path(argv[1])
    if not conf.is_file():
        print(f"no {conf}: nothing to update")
        return 0
    try:
        for line in run(conf, marks, leaving):
            print(line)
    except OSError as e:
        print(f"couldn't update {conf}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
