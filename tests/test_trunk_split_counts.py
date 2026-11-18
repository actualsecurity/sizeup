"""The split patch's lines counted (#647): trunk-recorder's "sizeup split" lines (SIZEUP_TR_SPLIT=log: would split, ID
flip, ID alias) and, with split on, "Unit ID changed on Voice Channel", are info level and name radios: never shipped,
counted per system with the calls they came in, and said once an hour (and when trunk-recorder is stopped) as one line
of counts, for the review that decides splitting per system; the same counts since sizeup started in Settings > Radios
(systems.Activity). Synthetic lines in trunk-recorder 5.2.0's shape (log_header, then the patch's own words,
deploy/station/install-linux.sh tr_patch); the radio IDs in them are made up."""

from __future__ import annotations

import json
import re

from sizeup.radio import systems, trunkrecd
from tests.test_p25_systems import MRRS, TR, prefs
from tests.test_trunk_watched import running_mrrs

A, B, C = "1234567", "7654321", "1234568"          # made-up radio IDs: never in anything said
IDS = (A, B, C)


def tr(sys_: str, msg: str, call: int = 41, tg: int = 9211, level: str = "info") -> str:
    """One of trunk-recorder 5.2.0's call lines (log_header, its colours included)."""
    return (f"[2026-11-18 09:00:00.000000] ({level})   [{sys_}]\t\x1b[0;34m{call}C\x1b[0m\tTG: \x1b[35m{tg:>10}\x1b[0m\t"
            f"Freq: 772.031250 MHz\t{msg}")


def start(sys_: str, call: int) -> str:
    return tr(sys_, "\x1b[32mStarting P25 Recorder Num [2]\x1b[0m\tTDMA: true\tSlot: 1\tQPSK: true", call)


def would(sys_: str, call: int) -> str:
    return tr(sys_, f"sizeup split (log): would split: prev {A} -> new {B}, 24000 samples into transmission", call)


def flip(sys_: str, call: int) -> str:
    return tr(sys_, f"sizeup split: ID flip {B} ~ {A}, the same talker", call)


def alias(sys_: str, call: int) -> str:
    return tr(sys_, f"sizeup split: ID alias {A} ~ {C}, 4000 samples into the talker's turn", call)


def cut(sys_: str, call: int) -> str:
    return tr(sys_, f"Unit ID changed on Voice Channel, ext: {B}\tprevious: {A}\t samples: 24000", call)


def no_ids(said: list[str]) -> bool:
    return not any(i in x for x in said for i in IDS) and not any(re.search(r"(?<![\d.])\d{5,}", x) for x in said)


def test_each_line_is_read_as_its_kind_its_system_and_its_call():
    for sys_ in ("westchester", "mrrs"):
        assert trunkrecd.split_line(would(sys_, 7)) == (sys_, "would", 7)
        assert trunkrecd.split_line(flip(sys_, 8)) == (sys_, "flip", 8)
        assert trunkrecd.split_line(alias(sys_, 9)) == (sys_, "alias", 9)
        assert trunkrecd.split_line(cut(sys_, 10)) == (sys_, "split", 10)
        assert trunkrecd.split_line(start(sys_, 11)) == (sys_, "calls", 11)
    for other in (tr("mrrs", "\x1b[33mConcluding Recorded Call\x1b[0m - Last Update: 4s"),
                  tr("mrrs", f"Unit ID set via Control Channel, ext: {A}\tcurrent: -1\t samples: 0"),
                  tr("mrrs", "Not recording talkgroup. Priority is 2 but only 1 recorders are available.", level="error"),
                  f"SIZEUP-UNIT mrrs {A} call 9211", ""):
        assert trunkrecd.split_line(other) is None
    assert trunkrecd.split_line(f"sizeup split: ID flip {B} ~ {A}") == ("", "flip", None)     # no header: still counted


def test_mrrs_counts_said_once_an_hour_on_the_monotonic_clock_never_its_lines():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append, "p25 mrrs")
    sh.started("log", 100.0)
    sh.tick(100.0)                                          # the hour starts with trunk-recorder
    t = 100.0
    for call in range(1, 41):                               # 40 calls ...
        sh.line(start("mrrs", call), t + call)
    sh.line(would("mrrs", 3), t + 50)                       # ... 2 of them would be cut, one twice
    sh.line(would("mrrs", 3), t + 51)
    sh.line(would("mrrs", 17), t + 52)
    sh.line(flip("mrrs", 5), t + 53)
    sh.line(alias("mrrs", 6), t + 54)
    sh.line(alias("mrrs", 7), t + 55)
    sh.tick(t + 3599)
    assert said == []                                       # neither the lines nor the count before the hour is over
    sh.tick(t + 3600)
    assert said == ["p25 mrrs: split (log) last hour: 3 would split, 1 ID flips, 2 aliases, in 40 calls "
                    "(5.0% of calls)"]
    sh.line(would("mrrs", 3), t + 3610)                     # call 3 again across the hour: its cut counted once
    sh.line(start("mrrs", 41), t + 3620)
    sh.tick(t + 7199)
    assert len(said) == 1
    sh.line(start("mrrs", 42), t + 7200)                    # a line rolls the hour as well as a tick
    assert said[1] == "p25 mrrs: split (log) last hour: 1 would split, 0 ID flips, 0 aliases, in 1 calls (0.0% of calls)"
    sh.tick(t + 7200 + 3600)                                # the next hour: one call, nothing cut
    assert said[2] == "p25 mrrs: split (log) last hour: 0 would split, 0 ID flips, 0 aliases, in 1 calls (0.0% of calls)"
    sh.tick(t + 7200 + 7200)                                # a quiet hour: nothing said
    assert len(said) == 3 and no_ids(said)


def test_county_and_mrrs_each_their_own_line_and_split_on_counts_its_cuts():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append)                     # the county's: tag "p25"
    sh.started("on", 0.0)
    sh.tick(0.0)
    for call in range(1, 5):
        sh.line(start("westchester", call), call)
    sh.line(cut("westchester", 2), 10)
    sh.line(cut("westchester", 2), 11)
    sh.line(flip("westchester", 3), 12)
    sh.line(start("mrrs", 1), 13)                          # another system in the same trunk-recorder: its own line
    sh.line(alias("mrrs", 1), 14)
    sh.tick(3600.0)
    assert said == ["p25 mrrs: split (on) last hour: 0 splits, 0 ID flips, 1 aliases, in 1 calls (0.0% of calls)",
                    "p25 westchester: split (on) last hour: 2 splits, 1 ID flips, 0 aliases, in 4 calls "
                    "(25.0% of calls)"]
    assert no_ids(said)


def test_off_counts_nothing_and_a_new_mode_says_the_old_ones_counts_first():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append, "p25 mrrs")
    sh.started("off", 0.0)
    for k in range(5):
        sh.line(start("mrrs", k), k)
    sh.tick(4000.0)
    sh.split_flush(4000.0)
    assert said == []
    sh.started("log", 5000.0)
    sh.line(start("mrrs", 1), 5001.0)
    sh.line(would("mrrs", 1), 5002.0)
    sh.started("on", 5601.0)                               # restarted with split on, no stop in between
    assert said == ["p25 mrrs: split (log) last 10 min: 1 would split, 0 ID flips, 0 aliases, in 1 calls "
                    "(100.0% of calls)"]
    sh.line(start("mrrs", 1), 5602.0)                      # call numbers start again: call 1 is a new call
    sh.line(cut("mrrs", 1), 5603.0)
    sh.split_flush(5902.0)
    assert said[1] == "p25 mrrs: split (on) last 5 min: 1 splits, 0 ID flips, 0 aliases, in 1 calls (100.0% of calls)"
    sh.split_flush(6000.0)                                 # nothing since: nothing said
    assert len(said) == 2 and no_ids(said)


def test_errors_and_watched_grants_ship_as_before():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append, "p25 mrrs")
    sh.watch({9212: "PD Tac"})
    sh.started("log", 0.0)
    sh.line(tr("mrrs", f"\x1b[31mNot Recording: ENCRYPTED\x1b[0m - src: {A}", tg=9212), 1.0)
    sh.line(tr("mrrs", "\x1b[32mStarting P25 Recorder Num [2]\x1b[0m", call=42, tg=9212), 2.0)   # watched: counted
    sh.line(tr("mrrs", "Not recording talkgroup. Priority is 2 but only 1 recorders are available.", level="error"), 3.0)
    sh.line(tr("mrrs", f"sizeup split (log): would split: prev {A} -> new {B}, 1 samples into transmission",
               level="warning"), 4.0)                      # a split line is never shipped, whatever its level
    assert said == ["p25 mrrs: TG 9212 PD Tac: grant (encrypted, not recorded)",
                    "p25 mrrs: trunk-recorder: (error) [mrrs] 41C TG: 9211 Freq: 772.031250 MHz Not recording "
                    "talkgroup. Priority is 2 but only 1 recorders are available."]
    sh.split_flush(62.0)
    assert said[2] == "p25 mrrs: split (log) last 60 s: 1 would split, 0 ID flips, 0 aliases, in 1 calls (100.0% of calls)"
    assert no_ids(said)


def test_the_system_supervisor_says_its_counts_when_trunk_recorder_is_stopped(tmp_path):
    """The real drain thread on MRRS's shipped config (split: log); its monotonic clock a fake one."""
    mono = [500.0]
    lines = [start("mrrs", 1), would("mrrs", 1), flip("mrrs", 1), start("mrrs", 2), alias("mrrs", 2)]
    mrrs, logs = running_mrrs(tmp_path, lines, mono)
    assert mrrs.step(1_763_400_000.0, mono[0])["state"] == "ok"
    mrrs._drain_thread.join(timeout=5)
    assert not [x for x in logs if "split" in x]
    mono[0] += 1200
    mrrs.stop("test")
    out = [x for x in logs if "split" in x]
    assert out == ["p25 mrrs: split (log) last 20 min: 1 would split, 1 ID flips, 1 aliases, in 2 calls "
                   "(50.0% of calls)"]
    mrrs.stop("again")                                      # nothing ran since: nothing more
    assert [x for x in logs if "split" in x] == out
    assert not any(i in x for x in logs for i in IDS)


def test_counted_since_sizeup_started_for_settings_radios(tmp_path):
    a = systems.Activity(MRRS, prefs(tmp_path), clock=lambda: 1_763_400_000.0)
    for line in (start("mrrs", 1), would("mrrs", 1), would("mrrs", 1), flip("mrrs", 1), start("mrrs", 2),
                 alias("mrrs", 2), start("mrrs", 3), cut("mrrs", 3), would("westchester", 9), start("westchester", 9)):
        a.feed_line(line)
    st = a.state()
    assert st["split"] == {"would": 2, "split": 1, "flip": 1, "alias": 1, "calls": 3, "cut": 2, "since": 1_763_400_000.0}
    assert not any(i in json.dumps(st) for i in IDS)
    js = (TR.parent.parent / "sizeup" / "web" / "static" / "radio-controls.js").read_text()
    assert "+ watched(x) + split(x)).join" in js and "${p.flip} ID flips, ${p.alias} aliases, in ${p.calls} calls" in js
