"""The station's wall-clock steps and boot id on its state post (sizeup/cloud/wallclock.py; #773, #776, #788). Fake clocks
only: a station's boot clock that runs fast, its wall clock corrected in small steps, and real steps of either sign."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from sizeup.app import make_app
from sizeup.cloud import wallclock
from sizeup.cloud.wallclock import WallSteps, read_boot_id
from tests.test_app import wait
from tests.test_cloud import free_port, station_cfg

T0 = 1_794_990_000.0


class Clocks:
    """Real time t (s); the boot clock runs `rate` x real time; the wall clock is real time plus `adj` (its corrections
    and steps), so (wall - boot) drifts by (1 - rate) a second and jumps by whatever adj does."""

    def __init__(self, rate: float = 1.05):
        self.t, self.rate, self.adj = 0.0, rate, 0.0

    def wall(self) -> float:
        return T0 + self.t + self.adj

    def boot(self) -> float:
        return 1000.0 + self.t * self.rate


def counter(clk: Clocks, said: list) -> WallSteps:
    return WallSteps(said.append, wall=clk.wall, boot=clk.boot, boot_id_path="/nonexistent/boot_id")


def run(clk: Clocks, w: WallSteps, seconds: float, every: float = 1.0, at=None) -> None:
    """Samples every `every` s of real time for `seconds`; at(t) is called before each (corrections, steps)."""
    end = clk.t + seconds
    while clk.t < end:
        clk.t += every
        if at is not None:
            at(clk.t)
        w.sample()


@pytest.mark.parametrize("rate", [1.02, 1.05, 0.95])
def test_drift_and_small_corrections_are_never_a_step(rate):
    """#788: the boot clock 2-5 % off and the wall clock pulled back by 1-2 s every 30 s, for an hour: 0 steps."""
    clk, said = Clocks(rate), []
    w = counter(clk, said)
    corrections = iter([-1.0, -2.0, -1.5, +1.0, -2.0, +2.0] * 50)

    def correct(t):
        if t % 30 == 0:
            clk.adj += next(corrections)
    run(clk, w, 3600, at=correct)
    assert w.steps == 0 and said == []


def test_drift_between_samples_up_to_5_s_apart_is_never_a_step():
    """5 % of 5 s is 0.25 s: samples as far apart as a step may be still see none, with a 2 s correction between."""
    clk, said = Clocks(1.05), []
    w = counter(clk, said)
    run(clk, w, 600, every=4.7, at=lambda t: setattr(clk, "adj", clk.adj - 2.0) if int(t) % 30 < 5 else None)
    assert w.steps == 0 and said == []


def test_a_forward_and_a_backward_step_are_counted_and_each_logged_once():
    clk, said = Clocks(1.05), []
    w = counter(clk, said)

    def steps(t):
        if t == 100:
            clk.adj += 60.0
        elif t == 200:
            clk.adj -= 15.0
        elif t % 30 == 0:
            clk.adj -= 1.5                 # (the small corrections go on around them)
    run(clk, w, 300, at=steps)
    assert w.steps == 2
    assert said == ["wall clock stepped +60 s", "wall clock stepped -15 s"]


def test_samples_more_than_5_s_apart_never_count():
    """A gap (a stall, a suspend, the link off) may hold a step: it is not counted, and the next sample is the new start."""
    clk, said = Clocks(1.0), []
    w = counter(clk, said)
    run(clk, w, 10)
    clk.t += 6.0                           # 6 s on the boot clock since the last sample ...
    clk.adj += 60.0                        # ... and the wall clock stepped meanwhile
    w.sample()
    run(clk, w, 10)
    assert w.steps == 0 and said == []
    clk.t += 5.0                           # 5 s apart is still "at most 5 s"
    clk.adj -= 30.0
    w.sample()
    assert w.steps == 1 and said == ["wall clock stepped -30 s"]


def test_a_step_of_5_s_or_less_is_not_one():
    clk, said = Clocks(1.0), []
    w = counter(clk, said)
    w.sample()
    for d in (4.99, -4.99, 4.9):
        clk.t += 1.0
        clk.adj += d
        w.sample()
    assert w.steps == 0
    clk.t += 1.0
    clk.adj += 5.2
    w.sample()
    assert w.steps == 1 and said == ["wall clock stepped +5 s"]


def test_a_clock_that_cannot_be_read_is_no_sample():
    clk, said = Clocks(1.0), []
    bad = {"on": False}

    def wall():
        if bad["on"]:
            raise OSError("no clock")
        return clk.wall()
    w = WallSteps(said.append, wall=wall, boot=clk.boot, boot_id_path="/nonexistent/boot_id")
    w.sample()
    clk.t += 1.0
    bad["on"] = True
    w.sample()                             # (never raises)
    bad["on"] = False
    clk.t += 1.0
    w.sample()
    assert w.steps == 0


def test_the_boot_id_is_read_or_none(tmp_path):
    f = tmp_path / "boot_id"
    f.write_text("5f0c9a3e-7b1d-4e2a-9c6f-0d8b2a4e1c37\n")
    assert read_boot_id(f) == "5f0c9a3e-7b1d-4e2a-9c6f-0d8b2a4e1c37"
    assert WallSteps(boot_id_path=f).boot_id == "5f0c9a3e-7b1d-4e2a-9c6f-0d8b2a4e1c37"
    assert read_boot_id(tmp_path / "missing") is None                       # (macOS: no /proc)
    f.write_text("\n")
    assert read_boot_id(f) is None
    f.write_bytes(b"\xff\xfe")
    assert read_boot_id(f) is None
    real = Path("/proc/sys/kernel/random/boot_id")
    assert read_boot_id() == (real.read_text().strip() if real.is_file() else None)


def test_the_boot_clock_is_boottime_where_there_is_one():
    if getattr(time, "CLOCK_BOOTTIME", None) is not None:
        assert abs(wallclock.boot_clock() - time.clock_gettime(time.CLOCK_BOOTTIME)) < 1.0
    else:
        assert abs(wallclock.boot_clock() - time.monotonic()) < 1.0


# -- across a restart (the count kept beside the outbox) ----------------------------------------------------------------

BOOT_A = "5f0c9a3e-7b1d-4e2a-9c6f-0d8b2a4e1c37"
BOOT_B = "0b7d1e4a-3c2f-4a9e-8d6b-1f5c0e7a9b24"


def boot_file(tmp_path, boot: str) -> Path:
    f = tmp_path / "boot_id"
    f.write_text(boot + "\n")
    return f


def kept(clk: Clocks, said: list, tmp_path, boot: str = BOOT_A) -> WallSteps:
    """A start of sizeup on these clocks, its count kept in tmp_path/wall_clock.json."""
    return WallSteps(said.append, wall=clk.wall, boot=clk.boot, boot_id_path=boot_file(tmp_path, boot),
                     path=tmp_path / "state" / wallclock.FILE)


def one_step_then_stop(clk: Clocks, said: list, tmp_path) -> WallSteps:
    w = kept(clk, said, tmp_path)
    run(clk, w, 100, at=lambda t: setattr(clk, "adj", clk.adj + 60.0) if t == 50 else None)
    w.stop()
    assert w.steps == 1
    return w


def test_a_restart_with_no_step_keeps_the_count(tmp_path):
    clk, said = Clocks(1.05), []
    one_step_then_stop(clk, said, tmp_path)
    clk.t += 120.0                         # sizeup stopped 2 min: the boot clock drifted 6 s (5 %) meanwhile
    clk.adj -= 2.0                         # ... and one small correction
    w = kept(clk, said, tmp_path)
    assert w.steps == 1 and said == ["wall clock stepped +60 s"]
    run(clk, w, 30)
    assert w.steps == 1
    d = json.loads((tmp_path / "state" / wallclock.FILE).read_text())
    assert d["boot_id"] == BOOT_A and d["wall_steps"] == 1 and {"offset", "boot_at"} <= set(d)


@pytest.mark.parametrize("step", [60.0, -15.0])
def test_a_restart_across_a_step_adds_one(tmp_path, step):
    clk, said = Clocks(1.05), []
    one_step_then_stop(clk, said, tmp_path)
    clk.t += 30.0
    clk.adj += step                        # the wall clock stepped while sizeup was stopped
    w = kept(clk, said, tmp_path)
    assert w.steps == 2
    assert said[-1] == f"wall clock stepped {step - 1.5:+.0f} s while sizeup was stopped"
    run(clk, w, 10)                        # (the first sample compares with the start: no second count)
    assert w.steps == 2 and len(said) == 2
    w.stop()
    assert kept(clk, said, tmp_path).steps == 2


def test_a_reboot_starts_from_0(tmp_path):
    clk, said = Clocks(1.05), []
    one_step_then_stop(clk, said, tmp_path)
    clk.t, clk.adj = 0.0, 999.0            # a new boot: its boot clock from the start again, the wall clock anywhere
    w = kept(clk, said, tmp_path, boot=BOOT_B)
    assert w.steps == 0 and len(said) == 1
    run(clk, w, 5)
    assert json.loads((tmp_path / "state" / wallclock.FILE).read_text())["boot_id"] == BOOT_B


def test_no_boot_id_keeps_nothing(tmp_path):
    """macOS: a reboot can't be told, so the count is never carried over (and never written)."""
    clk, said = Clocks(1.0), []
    w = WallSteps(said.append, wall=clk.wall, boot=clk.boot, boot_id_path=tmp_path / "none",
                  path=tmp_path / wallclock.FILE)
    run(clk, w, 5)
    w.stop()
    assert not (tmp_path / wallclock.FILE).exists()


@pytest.mark.parametrize("bad", ["", "{", "[]", "null", '{"boot_id": "%s"}' % BOOT_A,
                                 '{"boot_id": "%s", "wall_steps": "3", "offset": 1.0, "boot_at": 1.0}' % BOOT_A,
                                 '{"boot_id": "%s", "wall_steps": -1, "offset": 1.0, "boot_at": 1.0}' % BOOT_A,
                                 '{"boot_id": "%s", "wall_steps": true, "offset": 1.0, "boot_at": 1.0}' % BOOT_A,
                                 '{"boot_id": "%s", "wall_steps": 3, "offset": NaN, "boot_at": 1.0}' % BOOT_A,
                                 '{"boot_id": "%s", "wall_steps": 3, "offset": 1.0, "boot_at": 1e12}' % BOOT_A])
def test_a_corrupt_file_starts_from_0(tmp_path, bad):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / wallclock.FILE).write_text(bad)
    clk, said = Clocks(1.0), []
    w = kept(clk, said, tmp_path)
    assert w.steps == 0 and said == []
    run(clk, w, 2)
    assert json.loads((tmp_path / "state" / wallclock.FILE).read_text())["wall_steps"] == 0


def test_the_count_is_written_when_it_changes_and_at_most_every_60_s_otherwise(tmp_path, monkeypatch):
    clk, said = Clocks(1.0), []
    w = kept(clk, said, tmp_path)
    writes = []
    real = w.save
    monkeypatch.setattr(w, "save", lambda: (writes.append((clk.t, w.steps)), real())[1])
    run(clk, w, 1)                                   # the first sample: written
    run(clk, w, 150)
    assert [t for t, _ in writes] == [1.0, 61.0, 121.0]
    clk.t += 1.0
    clk.adj += 30.0
    w.sample()                                       # a step: written at once
    assert writes[-1] == (152.0, 1)
    run(clk, w, 10)
    assert len(writes) == 4
    w.stop()                                         # and at stop
    assert len(writes) == 5 and json.loads((tmp_path / "state" / wallclock.FILE).read_text())["wall_steps"] == 1


def test_a_write_that_fails_is_said_once_and_never_raises(tmp_path):
    clk, said = Clocks(1.0), []
    (tmp_path / "state").write_text("a file where the folder should be")
    w = kept(clk, said, tmp_path)
    run(clk, w, 3)
    clk.t += 1.0
    clk.adj += 30.0
    w.sample()
    w.stop()
    assert w.steps == 1
    assert [s for s in said if "can't keep" in s] == [said[0]] and said[1] == "wall clock stepped +30 s"


# -- on the state post -------------------------------------------------------------------------------------------------


class Capture:
    """The hub as the station's client sees it: every state post kept."""

    def __init__(self):
        self.posts: list[dict] = []
        self.lock = threading.Lock()

    def client(self, timeout):
        return self

    def request(self, method, path, body=None, timeout=None, **kw):
        path = path.split("?")[0]
        if path == "/api/station/state":
            with self.lock:
                self.posts.append(body)
            return {"ok": True, "build": ""}
        if path == "/api/station/jobs":
            time.sleep(0.05)
            return {"jobs": []}
        if path == "/api/station/events":
            return {"acked": max([e["seq"] for e in body["events"]] or [0])}
        return {}

    def close(self):
        pass

    def last(self) -> dict | None:
        with self.lock:
            return self.posts[-1] if self.posts else None


def test_every_state_post_carries_the_boot_id_and_the_step_count(tmp_path, monkeypatch):
    """Both on every post (with a status or not); the count goes up from the metrics sampler's 1 s tick, and the
    counter is the app's, so the link going off and on keeps it."""
    monkeypatch.setattr("sizeup.cloud.station.STATUS_S", 0.2)
    monkeypatch.setattr("sizeup.cloud.metrics.TICK_S", 0.05)
    monkeypatch.setattr(wallclock, "read_boot_id", lambda path=None: BOOT_A)       # (a Linux station's, anywhere)
    app = make_app(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"))
    fake, said = Capture(), []
    link = app.link
    link._client = fake.client
    link.wall.log = said.append
    assert link.metrics_link.tick == link.wall.sample and app.wall_clock is link.wall
    link.start()
    try:
        assert wait(lambda: fake.last() is not None, 5)
        post = fake.last()
        assert post["boot_id"] == BOOT_A and post["wall_steps"] == 0
        assert {"at", "boot_id", "wall_steps", "talking", "paging"} <= set(post)
        assert wait(lambda: link.wall._last is not None, 5)                 # (sampled once before the step)
        real = link.wall._wall
        link.wall._wall = lambda: real() + 60.0                              # the wall clock steps a minute ahead
        assert wait(lambda: (fake.last() or {}).get("wall_steps") == 1, 5)
        assert said == ["wall clock stepped +60 s"]
        assert wait(lambda: any(p.get("wall_steps") == 1 and "status" in p for p in list(fake.posts)), 5)
    finally:
        link.stop()
    from sizeup.cloud.station import StationLink
    again = StationLink(app, app.cfg, log=said.append, client=fake.client, outbox=link.outbox)
    assert again.wall is link.wall and again.wall.steps == 1
    saved = Path(app.cfg.cloud_outbox).with_name(wallclock.FILE)       # the stop saved it beside the outbox ...
    assert json.loads(saved.read_text())["wall_steps"] == 1 and json.loads(saved.read_text())["boot_id"] == BOOT_A
    after = WallSteps(said.append, wall=lambda: real() + 60.0, path=saved)   # ... and the next start carries it on
    assert after.steps == 1 and said == ["wall clock stepped +60 s"]
