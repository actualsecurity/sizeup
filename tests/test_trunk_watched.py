"""A watched talkgroup's grants (MRRS PD Tac, 9212: clear in its file, nothing recorded for a day): trunk-recorder's call
lines for the talkgroups of a system's "reception" "watch" list: a grant that wasn't recorded (encrypted, no recorder,
off, no source) said in the station log, at most one line per talkgroup a minute; every grant, recorded ones too,
counted for Settings > Radios (systems.Activity). Synthetic lines in trunk-recorder 5.2.0's shape (log_header, then
monitor_systems.cc / source.cc / p25_recorder_impl.cc); the radio IDs in them are made up."""

from __future__ import annotations

import csv
import json

from sizeup.radio import systems, trunkrecd
from tests.test_p25_systems import MRRS, TR, ask_system, prefs, service

PD, PD_TAC, EMS, OTHER = 9211, 9212, 9570, 9563
FAKE_RADIO = "1234567"


def tr(tg: int, msg: str, level: str = "info", call: int = 41) -> str:
    """One call line as trunk-recorder 5.2.0 writes it (its colours included)."""
    return (f"[2026-11-17 09:00:00.000000] ({level})   [mrrs]\t\x1b[0;34m{call}C\x1b[0m\tTG: \x1b[35m{tg:>10}\x1b[0m\t"
            f"Freq: 772.031250 MHz\t{msg}")


def grant(tg: int) -> str:
    return tr(tg, "\x1b[32mStarting P25 Recorder Num [2]\x1b[0m\tTDMA: true\tSlot: 1\tQPSK: true")


def encrypted(tg: int) -> str:
    return tr(tg, f"\x1b[31mNot Recording: ENCRYPTED\x1b[0m - src: {FAKE_RADIO}")


def no_recorder(tg: int) -> str:
    return tr(tg, "Not recording talkgroup. Priority is 2 but only 1 recorders are available.", "error")


# -- the shipped config ---------------------------------------------------------------------------------------------------


def test_pd_tac_is_watched_clear_and_second_for_a_recorder():
    conf = json.loads((TR / "systems" / "mrrs" / "config.json").read_text())
    assert conf["sizeup"]["reception"]["watch"] == [PD, PD_TAC, EMS] == MRRS.reception["watch"]
    with open(TR / "systems" / "mrrs" / "talkgroups.csv", newline="") as f:
        row = next(r for r in csv.DictReader(f) if r["Decimal"] == str(PD_TAC))
    # priority 2: the last free recorder of a side stays PD Dispatch's / EMS's (1); a starved PD Tac says "no recorder"
    assert (row["Alpha Tag"], row["Mode"], row["Priority"], row["Tier"]) == ("PD Tac", "T", "2", "text")
    assert not MRRS.talkgroups[PD_TAC]["encrypted"]
    assert trunkrecd.watched_talkgroups(TR / "systems" / "mrrs") == {PD: "PD Dispatch", PD_TAC: "PD Tac",
                                                                      EMS: "PC-Rye EMS"}
    assert trunkrecd.watched_talkgroups(TR) == {}                       # the county's: no watch list
    assert trunkrecd.watched_talkgroups(TR / "nowhere") == {}


# -- its lines ------------------------------------------------------------------------------------------------------------


def test_the_call_lines_are_read_as_what_became_of_the_grant():
    assert trunkrecd.call_line(grant(PD_TAC)) == (PD_TAC, "recording", "grant, recording")
    assert trunkrecd.call_line(encrypted(PD_TAC)) == (PD_TAC, "encrypted", "grant (encrypted, not recorded)")
    assert trunkrecd.call_line(no_recorder(PD_TAC)) == (PD_TAC, "no_recorder",
                                                        "not recorded, no recorder (priority 2, 1 free)")
    assert trunkrecd.call_line(tr(PD_TAC, "[ rtl=1 ] No Digital Recorders Available.", "error")) == (
        PD_TAC, "no_recorder", "not recorded, no recorder (none free)")
    assert trunkrecd.call_line(tr(PD_TAC, "Not recording talkgroup - Priority is -1 (Disabled).")) == (
        PD_TAC, "off", "not recorded, priority -1 (off)")
    assert trunkrecd.call_line(tr(PD_TAC, "\x1b[36mNot Recording: no source covering Freq\x1b[0m", "error")) == (
        PD_TAC, "no_source", "not recorded, no source covers 772.0312 MHz")
    for other in (tr(PD_TAC, "\x1b[33mConcluding Recorded Call\x1b[0m - Last Update: 4s"),
                  tr(PD_TAC, f"Unit ID set via Control Channel, ext: {FAKE_RADIO}\tcurrent: -1\t samples: 0"),
                  "[2026-11-17 09:00:00.000000] (info)   [mrrs]\tfreq: 769.906250 MHz\tControl Channel Message "
                  "Decode Rate: 38/sec, count:  114", f"SIZEUP-UNIT mrrs {FAKE_RADIO} call {PD_TAC}"):
        assert trunkrecd.call_line(other) is None


def test_a_watched_talkgroups_unrecorded_grants_reach_the_station_log_and_nothing_else_new_does():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append, "p25 mrrs")
    sh.watch({PD_TAC: "PD Tac", EMS: "PC-Rye EMS"})
    t = 1000.0
    sh.line(encrypted(PD_TAC), t)
    sh.line(grant(EMS), t)                                          # recorded: traffic already, never said
    sh.line(no_recorder(PD), t)                                     # unwatched: an error, as before (#547)
    sh.line(grant(PD), t)                                           # unwatched, info: nothing
    sh.line(encrypted(OTHER), t)                                    # unwatched, info: nothing
    assert said == ["p25 mrrs: TG 9212 PD Tac: grant (encrypted, not recorded)",
                    "p25 mrrs: trunk-recorder: (error) [mrrs] 41C TG: 9211 Freq: 772.031250 MHz Not recording "
                    "talkgroup. Priority is 2 but only 1 recorders are available."]
    sh.line(no_recorder(PD_TAC), t + 61)                            # a watched one's error: said once, its way
    assert said[-1] == "p25 mrrs: TG 9212 PD Tac: not recorded, no recorder (priority 2, 1 free)" and len(said) == 3
    for line in (tr(EMS, "Not recording talkgroup - Priority is -1 (Disabled)."),
                 tr(EMS, "\x1b[36mNot Recording: no source covering Freq\x1b[0m", "error")):
        sh.line(line, t + 200)
    assert said[3:] == ["p25 mrrs: TG 9570 PC-Rye EMS: not recorded, priority -1 (off)"]   # the 2nd: its minute
    assert not any(FAKE_RADIO in x for x in said)
    # nothing watched (the county's): its lines as before
    county: list[str] = []
    sh2 = trunkrecd.Shipper(county.append)
    for line in (encrypted(PD_TAC), grant(PD_TAC)):
        sh2.line(line, t)
    assert county == []


def test_one_line_per_talkgroup_a_minute_then_a_count():
    said: list[str] = []
    sh = trunkrecd.Shipper(said.append, "p25 mrrs")
    sh.watch({PD_TAC: "PD Tac", EMS: "PC-Rye EMS"})
    t = 5000.0
    sh.line(encrypted(PD_TAC), t)
    for k in range(5):                                              # a busy tac channel: held, counted
        sh.line(encrypted(PD_TAC), t + 5 + k)
    sh.line(no_recorder(PD_TAC), t + 20)
    sh.line(grant(PD_TAC), t + 20)                                  # recorded: neither said nor in the count
    sh.line(encrypted(EMS), t + 21)                                 # another talkgroup: its own minute
    sh.tick(t + 30)
    assert said == ["p25 mrrs: TG 9212 PD Tac: grant (encrypted, not recorded)",
                    "p25 mrrs: TG 9570 PC-Rye EMS: grant (encrypted, not recorded)"]
    sh.line(encrypted(PD_TAC), t + 60)
    assert said[-1] == "p25 mrrs: TG 9212 PD Tac: grant (encrypted, not recorded) (+6 more: 5 encrypted, 1 no recorder)"
    # a burst that goes quiet: its count once its minute is over, then nothing more
    sh.line(encrypted(PD_TAC), t + 70)
    sh.line(encrypted(PD_TAC), t + 80)
    for k in range(50):
        sh.line(grant(PD_TAC), t + 81 + k)                          # recorded calls: nothing, whatever their number
    sh.tick(t + 100)
    assert len(said) == 3
    sh.tick(t + 121)
    assert said[-1] == "p25 mrrs: TG 9212 PD Tac: +2 more: 2 encrypted"
    sh.line(no_recorder(PD_TAC), t + 150)                           # within a minute of the count: held
    sh.tick(t + 170)
    assert len(said) == 4
    sh.tick(t + 181)
    assert said[-1] == "p25 mrrs: TG 9212 PD Tac: +1 more: 1 no recorder"
    sh.tick(t + 900)
    assert len(said) == 5


class Out:
    """trunk-recorder's stdout: these lines, then its end."""

    def __init__(self, lines):
        self.lines = [x.encode() + b"\n" for x in lines] + [b""]

    def readline(self):
        return self.lines.pop(0) if self.lines else b""


def running_mrrs(tmp_path, lines, clock=None):
    """MRRS's SystemSupervisor on its shipped config, its fake trunk-recorder printing lines (the real drain thread;
    clock: its monotonic clock)."""
    _county, mrrs, pop, logs, _d = service(tmp_path)
    orig = pop.__call__

    def popen(argv, **kw):
        p = orig(argv, **kw)
        p.stdout = Out(lines)
        return p
    mrrs.popen = popen
    if clock is not None:
        mrrs.mono = lambda: clock[0]
    ask_system(tmp_path, {0: "pd-a"})
    return mrrs, logs


def test_the_system_supervisor_says_its_watched_grants_from_trunk_recorders_output(tmp_path):
    """The real drain thread on MRRS's own config (the watch list read as trunk-recorder starts)."""
    mrrs, logs = running_mrrs(tmp_path, [encrypted(PD_TAC), encrypted(PD_TAC), grant(OTHER), grant(PD),
                                         no_recorder(OTHER)])
    assert mrrs.step(100.0)["state"] == "ok"
    mrrs._drain_thread.join(timeout=5)
    out = [x for x in logs if x.startswith("p25 mrrs: TG ")]
    assert out == ["p25 mrrs: TG 9212 PD Tac: grant (encrypted, not recorded)"]
    assert any(x.startswith("p25 mrrs: trunk-recorder: (error)") and "TG: 9563" in x for x in logs)   # unwatched
    assert not any(FAKE_RADIO in x for x in logs)


def test_the_held_count_goes_by_the_monotonic_clock_not_the_walls(tmp_path):
    """The drain stamps its lines with the supervisor's monotonic clock and each step ticks with the same one, so a
    wall clock far from it (any real one) neither flushes a count at once nor never."""
    mono = [500.0]
    wall = 1_763_400_000.0
    mrrs, logs = running_mrrs(tmp_path, [encrypted(PD_TAC), encrypted(PD_TAC), encrypted(PD_TAC)], mono)
    assert mrrs.step(wall, mono[0])["state"] == "ok"
    mrrs._drain_thread.join(timeout=5)
    out = lambda: [x for x in logs if x.startswith("p25 mrrs: TG ")]   # noqa: E731
    assert out() == ["p25 mrrs: TG 9212 PD Tac: grant (encrypted, not recorded)"]
    mono[0] += 30
    mrrs.step(wall + 30, mono[0])
    assert len(out()) == 1                                           # the minute isn't over on the monotonic clock
    mono[0] += 31
    mrrs.step(wall + 61, mono[0])
    assert out()[-1] == "p25 mrrs: TG 9212 PD Tac: +2 more: 2 encrypted"


# -- counted for Settings > Radios ------------------------------------------------------------------------------------------


def test_watched_grants_are_counted_by_what_became_of_them(tmp_path):
    a = systems.Activity(MRRS, prefs(tmp_path), clock=lambda: 1_763_400_000.0)
    for line in (encrypted(PD_TAC), encrypted(PD_TAC), grant(PD_TAC), no_recorder(PD_TAC), grant(EMS),
                 encrypted(OTHER), grant(OTHER)):
        a.feed_line(line)
    w = {x["tg"]: x for x in a.state()["watched"]}
    assert set(w) == {PD, PD_TAC, EMS}
    assert {k: w[PD_TAC][k] for k in ("name", "grants", "recording", "encrypted", "no_recorder", "off", "no_source")} == {
        "name": "PD Tac", "grants": 4, "recording": 1, "encrypted": 2, "no_recorder": 1, "off": 0, "no_source": 0}
    assert (w[EMS]["grants"], w[EMS]["recording"], w[PD]["grants"]) == (1, 1, 0)
    assert w[PD_TAC]["since"] == 1_763_400_000.0
    assert a.state()["encrypted"] == []                              # a clear talkgroup's: never activity rows
    assert FAKE_RADIO not in json.dumps(a.state())
    # Settings > Radios: a watched talkgroup's encrypted grants on the system's line, every count in its hover
    js = (TR.parent.parent / "sizeup" / "web" / "static" / "radio-controls.js").read_text()
    assert "(x.watched || []).filter((w) => w.encrypted).map((w) => ` · TG ${w.tg} encrypted ×${w.encrypted}`)" in js
    assert "+ watched(x)).join" in js and "${w.recording} recorded, ${w.encrypted} encrypted" in js


def test_a_config_it_cannot_read_watches_nothing(tmp_path):
    for bad in ("[]", "{\"sizeup\": 3}", "{\"sizeup\": {\"reception\": {\"watch\": [\"x\", true, 9212]}}, \"systems\": 4}",
                "{\"sizeup\": {\"reception\": {\"watch\": [9212]}}, \"systems\": [{\"talkgroupsFile\": \"gone.csv\"}]}", "{"):
        (tmp_path / "config.json").write_text(bad)
        assert trunkrecd.watched_talkgroups(tmp_path) in ({}, {PD_TAC: ""})
