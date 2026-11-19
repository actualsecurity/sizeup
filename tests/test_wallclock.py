"""The station's wall-clock steps and boot id on its state post (sizeup/cloud/wallclock.py; #773, #776, #788). Fake clocks
only: a station's boot clock that runs fast, its wall clock corrected in small steps, and real steps of either sign."""
from __future__ import annotations

import dataclasses
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


def test_a_step_inside_a_gap_over_5_s_counts():
    """A gap (a stall, a suspend, the hub link off) is compared too: a 6 s gap holding a 60 s step is one step."""
    clk, said = Clocks(1.05), []
    w = counter(clk, said)
    run(clk, w, 10)
    clk.t += 6.0                           # 6 s since the last sample ...
    clk.adj += 60.0                        # ... and the wall clock stepped meanwhile
    w.sample()
    run(clk, w, 10)                        # (once: the samples after it compare with the new offset)
    assert w.steps == 1 and said == ["wall clock stepped +60 s"]


@pytest.mark.parametrize("rate", [1.05, 0.95])
def test_a_2_minute_gap_with_drift_only_is_not_a_step(rate):
    """5 % of 2 min is 6 s (more than STEP_S), within the 5 s + 10 % x 120 s allowed; a 2 s correction in it too."""
    clk, said = Clocks(rate), []
    w = counter(clk, said)
    run(clk, w, 10)
    clk.t += 120.0
    clk.adj -= 2.0
    w.sample()
    run(clk, w, 10)
    assert w.steps == 0 and said == []
    clk.t += 120.0                         # ... but a 20 s step in a 2 minute gap is beyond it (6 + 20 > 17)
    clk.adj += 20.0 if rate < 1 else -20.0
    w.sample()
    assert w.steps == 1


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


def test_the_boot_clock_is_boottime_else_clock_monotonic_which_counts_sleep():
    """Linux: CLOCK_BOOTTIME. macOS: CLOCK_MONOTONIC (it counts sleep; time.monotonic() there does not)."""
    if getattr(time, "CLOCK_BOOTTIME", None) is not None:
        assert abs(wallclock.boot_clock() - time.clock_gettime(time.CLOCK_BOOTTIME)) < 1.0
    else:
        assert wallclock._CLOCKS == (time.CLOCK_MONOTONIC,)
        assert abs(wallclock.boot_clock() - time.clock_gettime(time.CLOCK_MONOTONIC)) < 1.0


def test_interleaved_samplers_never_count_one_step_twice():
    """Two threads sample (the 1 s tick and the state thread): one whose reading is older can never put it back over
    a newer one (the clocks are read under the lock), so one +60 s step is one step."""
    clk, said = Clocks(1.0), []
    clk.t = 10.0
    hold, inside = threading.Event(), threading.Event()

    def wall():
        v = clk.wall()
        if threading.current_thread().name == "slow-sampler":
            inside.set()
            hold.wait(2.0)                 # (its reading, taken before the step, held back)
        return v
    w = WallSteps(said.append, wall=wall, boot=clk.boot, boot_id_path="/nonexistent/boot_id")
    w.sample()
    clk.t += 1.0
    a = threading.Thread(target=w.sample, name="slow-sampler")
    a.start()
    assert inside.wait(2.0)
    clk.t += 1.0
    clk.adj += 60.0                        # the step, while the slow one holds its older reading
    b = threading.Thread(target=w.sample, name="fast-sampler")
    b.start()
    time.sleep(0.1)
    hold.set()
    a.join(5.0)
    b.join(5.0)
    for _ in range(3):
        clk.t += 1.0
        w.sample()
    assert w.steps == 1 and said == ["wall clock stepped +60 s"]


def test_a_sample_from_before_the_last_one_changes_nothing():
    """A boot-clock reading earlier than the last (never on one clock): not a sample, the last one kept."""
    clk, said = Clocks(1.0), []
    w = counter(clk, said)
    clk.t = 10.0
    w.sample()
    kept_last = w._last
    clk.t = 9.0
    clk.adj += 60.0
    assert w.sample() == (clk.wall(), 0) and w._last == kept_last and w.steps == 0


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


def test_no_state_folder_sends_a_null_boot_id(tmp_path):
    """No path (a ":memory:" outbox): the count can't outlive a restart, so the post's boot_id is null (unknown)."""
    w = WallSteps(boot_id_path=boot_file(tmp_path, BOOT_A))
    assert w.boot_id == BOOT_A and w.post_boot_id is None
    assert WallSteps(boot_id_path=boot_file(tmp_path, BOOT_A), path=tmp_path / wallclock.FILE).post_boot_id == BOOT_A


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
    monkeypatch.setattr(w, "save", lambda wait=True: (writes.append((clk.t, w.steps)), real(wait=wait))[1])
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


def test_a_write_that_fails_is_said_loudly_once_and_on_health_and_never_raises(tmp_path):
    """Saves that keep failing: a restart then takes back an older count (the contract's "unless saves fail"); said
    once in the log (ERROR) and once as a note for Health, again only after a save worked in between."""
    clk, said, notes = Clocks(1.0), [], []
    (tmp_path / "state").write_text("a file where the folder should be")
    w = WallSteps(said.append, wall=clk.wall, boot=clk.boot, boot_id_path=boot_file(tmp_path, BOOT_A),
                  path=tmp_path / "state" / wallclock.FILE, note=notes.append)
    run(clk, w, 3)
    clk.t += 1.0
    clk.adj += 30.0
    w.sample()
    w.stop()
    assert w.steps == 1
    failed = [s for s in said if "can't save" in s]
    assert len(failed) == 1 and "ERROR" in failed[0] and "wall clock stepped +30 s" in said
    assert notes == ["wall clock: can't save the step count (a restart may lose steps)"]
    (tmp_path / "state").unlink()          # the disk is back: a save works ...
    run(clk, w, 1)
    assert json.loads((tmp_path / "state" / wallclock.FILE).read_text())["wall_steps"] == 1
    (tmp_path / "state" / wallclock.FILE).unlink()
    (tmp_path / "state").rmdir()
    (tmp_path / "state").write_text("broken again")
    clk.t += 1.0
    clk.adj += 30.0
    w.sample()                             # ... and a new run of failures is said again
    assert len([s for s in said if "can't save" in s]) == 2 and len(notes) == 2


def test_a_slow_disk_never_holds_a_sample_and_stop_waits_for_it_at_most_2_s(tmp_path, monkeypatch):
    """The write is outside the lock: while the tick's write hangs, the state thread's look still returns at once
    with the new count; stop() waits for the write in flight at most 2 s."""
    import types
    clk, said = Clocks(1.0), []
    w = kept(clk, said, tmp_path)
    run(clk, w, 1)                                   # (the first write)
    slow, writing = threading.Event(), threading.Event()

    def replace(a, b):
        writing.set()
        slow.wait(10.0)
        os.replace(a, b)
    import os
    monkeypatch.setattr(wallclock, "os", types.SimpleNamespace(replace=replace, getpid=os.getpid))
    clk.t += 1.0
    clk.adj += 30.0
    tick = threading.Thread(target=w.sample)         # the tick: a step, so a write, which hangs
    tick.start()
    assert writing.wait(2.0)
    clk.t += 1.0
    t0 = time.monotonic()
    assert w.sample(save=False) == (clk.wall(), 1)   # the state thread's look: not held
    assert time.monotonic() - t0 < 0.5
    t0 = time.monotonic()
    w.stop()                                         # the write in flight: waited for 2 s at most
    assert time.monotonic() - t0 < 3.0
    slow.set()
    tick.join(5.0)
    assert json.loads((tmp_path / "state" / wallclock.FILE).read_text())["wall_steps"] == 1


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
    assert link.wall.post_boot_id == BOOT_A                         # (kept beside the outbox)
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
        stepped = [p for p in list(fake.posts) if p["at"] > time.time() + 30]
        assert stepped and all(p["wall_steps"] == 1 for p in stepped)        # never a stepped `at` with the old count
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


def test_a_stalled_sampler_never_hides_a_step_between_posts(tmp_path, monkeypatch):
    """The 1 s tick stalled (30 s, say) while posts go on: the state thread's own look counts a 7 s step (over a 30 s
    gap the allowance would be 8 s), and the post carries it with the stepped `at`."""
    monkeypatch.setattr("sizeup.cloud.station.STATUS_S", 0.2)
    monkeypatch.setattr("sizeup.cloud.metrics.TICK_S", 3600.0)       # the sampler never ticks
    monkeypatch.setattr(wallclock, "read_boot_id", lambda path=None: BOOT_A)
    app = make_app(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"))
    fake, said = Capture(), []
    link = app.link
    link._client = fake.client
    link.wall.log = said.append
    link.start()
    try:
        assert wait(lambda: len(fake.posts) >= 2, 5)
        real = link.wall._wall
        link.wall._wall = lambda: real() + 7.0
        assert wait(lambda: (fake.last() or {}).get("wall_steps") == 1, 5)
        assert said == ["wall clock stepped +7 s"]
        assert all(p["wall_steps"] == 1 for p in list(fake.posts) if p["at"] > time.time() + 5)
    finally:
        link.stop()


def test_a_station_without_a_state_folder_posts_a_null_boot_id_and_notes_go_to_health(tmp_path, monkeypatch):
    monkeypatch.setattr(wallclock, "read_boot_id", lambda path=None: BOOT_A)
    cfg = dataclasses.replace(station_cfg(tmp_path, f"http://127.0.0.1:{free_port()}"), cloud_outbox=":memory:")
    app = make_app(cfg)
    assert app.link.wall.path is None and app.link.wall.post_boot_id is None
    app.link.wall.note("wall clock: can't save the step count (a restart may lose steps)")
    assert app.link.outbox.notes[-1]["text"] == "wall clock: can't save the step count (a restart may lose steps)"
    assert app.link.status()["outbox"]["notes"][-1]["text"].startswith("wall clock:")
