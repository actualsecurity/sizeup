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

Done ones are listed in a marks file (/etc/sizeup/config-updates, one name per line), so one taken out
by hand stays out. The config is only ever added to (tones-47 also changes the two values of the one
entry it names): the text is kept as it is, the result must parse
(tomllib) and say what was meant, else nothing is written. The old file is kept beside it
(config.toml.before-<name>), the new one replaces it with one rename and the same owner and mode.

Run by the system python3 (stdlib only, like autoupdate.py), from the release being switched to:

    python3 -I configupdate.py /etc/sizeup/config.toml /etc/sizeup/config-updates

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


UPDATES = (("fg2", add_fg2), ("tones-47", fix_tones))


def run(conf: Path, marks: Path) -> list[str]:
    """Every update not in marks, applied to conf in turn. Lines to say."""
    done = set(marks.read_text().split()) if marks.exists() else set()
    todo = [(n, f) for n, f in UPDATES if n not in done]
    if not todo:
        return []
    text = conf.read_text()
    out = []
    for name, fn in todo:
        try:
            new, said = fn(text)
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
    if len(argv) != 2:
        print("usage: configupdate.py CONFIG MARKS", file=sys.stderr)
        return 2
    conf, marks = Path(argv[0]), Path(argv[1])
    if not conf.is_file():
        print(f"no {conf}: nothing to update")
        return 0
    try:
        for line in run(conf, marks):
            print(line)
    except OSError as e:
        print(f"couldn't update {conf}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
