"""#677 / #709: where the station's STT inputs are made (sizeup/cloud/sttencode.py: the sizeup-stt-encode service, a
child process, else the station's thread; the same bytes every way), the worker's framing and its checks, a slow worker
vs a dead one, and the hold while whisper is behind on live traffic (never the pre-alerts or fire traffic, at most
HOLD_MAX_S). Synthetic audio only."""

from __future__ import annotations

import io
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup.cloud import flac, sttencode, sttinput
from sizeup.cloud.station import StationLink
from sizeup.incidents import Traffic
from sizeup.radio.fm import RATE
from sizeup.stt import BEHIND_S, CONTEXT, CONTEXT_FALLBACK, HIGH, LIVE, LOW, RERUN, URGENT
from tests.test_app import voice, wait
from tests.test_stt_input import _transmission, offline_station, stt_rows

PRE, FIRE, NORMAL, LANE_LOW = sttinput.PRE, sttinput.FIRE, sttinput.NORMAL, sttinput.LOW


def _tx(tid, a, fm=True):
    t = Traffic(tid, "FG1", 0.0, len(a) / RATE, "")
    t.fm = fm                                                               # type: ignore[attr-defined]
    return t


def _made_by(mode, items, **kw):
    out, done = [], threading.Event()

    def sink(d, data, meta, prio):
        out.append((d["id"], data, meta, prio))
        if len(out) == len(items):
            done.set()
    enc = sttinput.Encoder(sink, log=kw.pop("log", lambda m: None), mode=mode, **kw)
    try:
        for tid, a, fm, prio in items:
            assert enc.submit(_tx(tid, a, fm), a, prio)
        assert done.wait(60), (enc.where, enc.dropped)
        return out, enc.where, enc
    finally:
        enc.stop()


def _items():
    # (NORMAL: the worker's; PRE / FIRE are always made on the station's own thread)
    return [(f"Ops-{k}", voice(1.0 + k / 2, seed=40 + k), k % 2 == 0, NORMAL) for k in range(4)]


@pytest.fixture
def short_dir():
    d = tempfile.mkdtemp(dir="/tmp", prefix="sz-enc-")                     # (a Unix socket's path: short)
    os.chmod(d, 0o755)
    yield d
    for f in os.listdir(d):
        os.unlink(os.path.join(d, f))
    os.rmdir(d)


@pytest.fixture
def encode_service(monkeypatch, short_dir):
    """The sizeup-stt-encode service (`--serve <socket>`), as a child of this test's own."""
    path = f"{short_dir}/encode.sock"
    proc = subprocess.Popen([sys.executable, "-m", "sizeup.cloud.sttencode", "--serve", path])
    try:
        assert wait(lambda: os.path.exists(path), 20)
        monkeypatch.setenv(sttencode.SOCKET_ENV, path)
        yield proc, path
    finally:
        proc.kill()                                                         # (our own child)
        proc.wait(5)


# -- the same bytes, wherever they are made ---------------------------------------------------------------------------


def test_the_worker_process_and_the_service_make_the_very_same_flac_as_the_thread(encode_service):
    items = _items()
    by_thread, where_t, _ = _made_by("thread", items)
    by_child, where_c, enc = _made_by("process", items)
    by_service, where_s, _ = _made_by("auto", items)
    assert (where_t, where_c, where_s) == ("thread", "process", "service")
    assert by_child == by_thread and by_service == by_thread                # every byte, the metadata, the order
    assert enc._worker is None                                              # (stopped with the encoder)
    assert np.array_equal(flac.decode(by_service[0][1])[0], sttinput.stt_pcm(items[0][1], True))
    # float32 samples go as float32 (radio_voice reads them as float64 either way: the same bytes)
    a32 = items[0][1].astype(np.float32)
    assert _made_by("auto", [("Ops-f32", a32, True, NORMAL)])[0] == _made_by("thread", [("Ops-f32", a32, True, NORMAL)])[0]


def test_the_service_socket_is_ours_alone(encode_service):
    _proc, path = encode_service
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(path)
    uid = sttencode.peer_uid(s)
    assert uid in (None, os.getuid())                                       # (Linux: the worker's own uid)
    s.close()


def test_without_the_encode_service_the_thread_makes_them_and_says_why(monkeypatch, tmp_path):
    monkeypatch.setenv(sttencode.SOCKET_ENV, str(tmp_path / "none.sock"))
    items = _items()[:2]
    out, where, _ = _made_by("auto", items)
    assert where == "thread (no sizeup-stt-encode service)" and out == _made_by("thread", items)[0]


# -- the frames: bounded, checked before anything is allocated --------------------------------------------------------


def _frame(head: dict, payload_len: int, payload: bytes = b"") -> bytes:
    h = __import__("json").dumps(head).encode()
    return struct.pack(">II", len(h), payload_len) + h + payload


def test_a_frame_too_big_or_badly_formed_is_refused_before_it_is_read():
    # a payload claimed over MAX_FRAME, or a header over MAX_HEADER: refused from the 8 bytes alone (nothing more read)
    with pytest.raises(sttencode.FrameError):
        sttencode.read_frame(io.BytesIO(struct.pack(">II", 10, sttencode.MAX_FRAME + 1)))
    with pytest.raises(sttencode.FrameError):
        sttencode.read_frame(io.BytesIO(struct.pack(">II", sttencode.MAX_HEADER + 1, 0)))
    with pytest.raises(sttencode.FrameError):
        sttencode.read_frame(io.BytesIO(_frame([1, 2], 0)))                 # not an object
    good = {"proto": sttencode.PROTO, "op": "encode", "desc": {"id": "FG1-1", "kind": "traffic"}, "fm": True,
            "offset": 0.0, "rate": 16000, "samples": 100, "dtype": "<f8"}
    for bad, n in (({**good, "samples": 101}, 800), ({**good, "dtype": "|O"}, 800), ({**good, "rate": 8000}, 800),
                   ({**good, "samples": sttinput.MAX_SECONDS * 16000 + 1}, 8), ({**good, "desc": {"kind": "x"}}, 800),
                   ({**good, "desc": {"kind": "traffic", "x": [1]}}, 800), ({**good, "proto": 1}, 800)):
        f = io.BytesIO(_frame(bad, n))                                      # (the payload itself never sent)
        with pytest.raises(sttencode.FrameError):
            sttencode.read_frame(f, sttencode.MAX_FRAME, sttencode._check_encode)


def test_the_service_answers_a_bad_frame_and_closes_and_refuses_another_release(encode_service):
    _proc, path = encode_service

    def conn():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(path)
        return s, s.makefile("rb"), s.makefile("wb")
    s, fin, fout = conn()
    sttencode.write_frame(fout, {"proto": sttencode.PROTO, "op": "hello", "code": "another-release"})
    assert sttencode.read_frame(fin, 0)[0]["refused"] == "version"
    s.close()
    s, fin, fout = conn()
    sttencode.write_frame(fout, {"proto": sttencode.PROTO, "op": "hello", "code": sttencode.CODE})
    assert sttencode.read_frame(fin, 0)[0]["ok"] is True
    fout.write(struct.pack(">II", 10, 1 << 31))                             # a 2 GB payload claimed
    fout.flush()
    head, _ = sttencode.read_frame(fin, 0)
    assert head["error"].startswith("refused: frame too big")
    assert fin.read(1) == b""                                               # ... and closed
    s.close()


def test_another_releases_worker_leaves_it_to_the_thread_said_once(encode_service, monkeypatch):
    monkeypatch.setattr(sttencode, "CODE", "0123456789abcdef")              # (this station: another release)
    logs = []
    items = _items()
    out, where, _ = _made_by("auto", items, log=logs.append)
    monkeypatch.undo()
    assert where.startswith("thread (another release's worker") and out == _made_by("thread", items)[0]
    assert sum("another release" in m for m in logs) == 1


# -- a slow worker vs a dead one --------------------------------------------------------------------------------------


class FakeWorker:
    """A service that says hello, then never answers ("slow": hello-only), hangs up mid-request ("dead"), or answers
    late with the real thing ("late": after `delay`)."""

    def __init__(self, path: str, how: str, delay: float = 0.0):
        self.how, self.delay, self.requests = how, delay, 0
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        self.srv.bind(path)
        os.umask(old)
        self.srv.listen(4)
        self.conns = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        fin, fout = c.makefile("rb"), c.makefile("wb")
        try:
            sttencode.read_frame(fin, 0)
            sttencode.write_frame(fout, {"ok": True, "code": sttencode.CODE})
            while True:
                head, payload = sttencode.read_frame(fin, sttencode.MAX_FRAME)
                self.requests += 1
                if self.how == "dead":
                    c.shutdown(socket.SHUT_RDWR)
                    return
                if self.how == "slow":
                    time.sleep(120)                                         # never an answer
                    return
                time.sleep(self.delay)
                got = sttinput._blob(head["desc"], head["fm"], head["offset"], np.frombuffer(payload, head["dtype"]))
                sttencode.write_frame(fout, {"ok": True, "meta": got[1]}, got[0])
        except (OSError, EOFError, struct.error):
            pass

    def close(self):
        self.srv.close()
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()


def test_a_hello_only_worker_never_delays_pre_or_fire_and_never_gets_a_clip_twice(short_dir, monkeypatch):
    path = f"{short_dir}/encode.sock"
    fake = FakeWorker(path, "slow")
    monkeypatch.setenv(sttencode.SOCKET_ENV, path)
    monkeypatch.setattr(sttinput, "WAIT_CAP_S", 0.3)
    made, logs = [], []
    enc = sttinput.Encoder(lambda d, data, meta, prio: made.append((d["id"], time.monotonic())), log=logs.append,
                           mode="auto")
    try:
        a = voice(1.0, seed=1)
        assert enc.submit(_tx("Ops-1", a), a, NORMAL) and enc.submit(_tx("Ops-2", a), a, NORMAL)
        assert wait(lambda: fake.requests == 1, 10)                         # Ops-1 in flight: no answer, ever
        for tid, prio in (("South2-1", PRE), ("FG1-1", FIRE)):
            t0 = time.monotonic()
            assert enc.submit(_tx(tid, a), a, prio)
            assert wait(lambda: any(m[0] == tid for m in made), 5)
            assert next(m[1] for m in made if m[0] == tid) - t0 < 1.0       # at once, on the station's own thread
        assert wait(lambda: enc.unhealthy, 5)                               # three waits without an answer: unhealthy
        time.sleep(1.0)
        assert fake.requests == 1                                           # never sent again, never a second clip
        assert [m[0] for m in made] == ["South2-1", "FG1-1"]                # Ops-*: never made on the station
        assert enc.stats()["queued"] == 1 and enc.dropped == {"queue": 0, "too_big": 0, "failed": 0}
        assert sum("unhealthy" in m for m in logs) == 1 and sum("is slow" in m for m in logs) == 1   # said once each
        t0 = time.monotonic()
        enc.stop()                                                          # promptly, mid-wait on the worker
        assert time.monotonic() - t0 < 3.0
    finally:
        enc.stop()
        fake.close()


def test_the_worker_is_unhealthy_after_long_enough_without_an_answer_and_well_again_when_it_answers(
        short_dir, monkeypatch):
    path = f"{short_dir}/encode.sock"
    fake = FakeWorker(path, "late", delay=2.0)
    monkeypatch.setenv(sttencode.SOCKET_ENV, path)
    monkeypatch.setattr(sttinput, "WAIT_CAP_S", 0.2)
    monkeypatch.setattr(sttinput, "WORKER_SLOW", 100)                       # (the clock trips it, not the count)
    monkeypatch.setattr(sttinput, "UNHEALTHY_S", 1.0)
    made, logs = [], []
    enc = sttinput.Encoder(lambda d, data, meta, prio: made.append((d["id"], data)), log=logs.append, mode="auto")
    try:
        a = voice(1.0, seed=2)
        assert enc.submit(_tx("Ops-1", a), a, NORMAL)
        assert wait(lambda: enc.unhealthy, 5)
        assert wait(lambda: made and not enc.unhealthy, 10)                 # the late answer: taken, well again
        assert made == [("Ops-1", _made_by("thread", [("Ops-1", a, True, NORMAL)])[0][0][1])] and fake.requests == 1
        assert any("unhealthy" in m for m in logs) and any("answers again" in m for m in logs)
    finally:
        enc.stop()
        fake.close()


def test_a_connection_the_service_would_have_closed_for_silence_is_replaced_before_the_next_clip(
        encode_service, monkeypatch):
    monkeypatch.setattr(sttencode, "SERVER_IDLE_S", 0.5)                    # (the station's side of it)
    made, logs = [], []
    enc = sttinput.Encoder(lambda d, data, meta, prio: made.append(d["id"]), log=logs.append, mode="auto")
    try:
        a = voice(0.5, seed=3)
        assert enc.submit(_tx("Ops-1", a), a, NORMAL) and wait(lambda: made == ["Ops-1"], 20)
        first = enc._worker
        time.sleep(0.6)
        assert enc.submit(_tx("Ops-2", a), a, NORMAL) and wait(lambda: made == ["Ops-1", "Ops-2"], 20)
        assert enc._worker is not first and enc.where == "service" and enc._fails == 0
        assert not any("is gone" in m for m in logs)
    finally:
        enc.stop()


def test_the_code_hash_is_made_at_import_and_has_numpys_version(monkeypatch):
    assert sttencode.code_version() == sttencode.CODE == sttencode._code_version()
    monkeypatch.setattr(sttencode.np, "__version__", "0.0.0-another")
    assert sttencode._code_version() != sttencode.CODE                      # another numpy: another worker's bytes
    assert sttencode.code_version() == sttencode.CODE                       # (this process's: as imported)


def test_a_dead_worker_hands_the_clip_to_the_thread_and_three_in_a_row_the_rest(short_dir, monkeypatch):
    path = f"{short_dir}/encode.sock"
    fake = FakeWorker(path, "dead")
    monkeypatch.setenv(sttencode.SOCKET_ENV, path)
    logs = []
    items = _items()
    try:
        out, where, _ = _made_by("auto", items, log=logs.append)
    finally:
        fake.close()
    assert out == _made_by("thread", items)[0]                              # nothing lost
    assert fake.requests == 3 and where.startswith("thread (the worker failed 3 times")
    assert sum("is gone" in m for m in logs) == 1 and sum("failed 3 times" in m for m in logs) == 1


def test_a_worker_that_cannot_start_or_is_killed_is_replaced_and_nothing_is_lost(monkeypatch):
    items = _items()
    want = _made_by("thread", items)[0]
    monkeypatch.setattr(sttinput.sys, "executable", "/nonexistent/python")     # (its program gone)
    out, where, _ = _made_by("process", items)
    monkeypatch.undo()
    assert out == want and where.startswith("thread (the worker failed 3 times")
    # a child killed between two clips: that clip on the thread, a new child for the next
    out = []
    enc = sttinput.Encoder(lambda d, data, meta, prio: out.append((d["id"], data, meta, prio)), log=lambda m: None,
                           mode="process")
    try:
        for k in range(3):
            if k == 1:
                enc._worker.proc.kill()                                     # (our own child)
                enc._worker.proc.wait(5)
            enc.submit(_tx(items[k][0], items[k][1], items[k][2]), items[k][1], NORMAL)
            assert wait(lambda: len(out) == k + 1, 30)
            if k == 1:
                assert enc._worker is None and enc._fails == 1
        assert out == want[:3] and enc._worker is not None and enc._fails == 0 and enc.where == "process"
    finally:
        enc.stop()


# -- held while whisper is behind on live traffic ---------------------------------------------------------------------


def _q(*entries):
    """A stand-in for the whisper queue's insides (sizeup/stt.py SttQueue: [priority, seq, item, audio, channel, at,
    fallback]), at clock 10000."""
    return SimpleNamespace(clock=lambda: 10_000.0, _cv=threading.Lock(),
                           _items=[[p, k, None, None, "", 10_000.0 - age, False] for k, (p, age) in enumerate(entries)])


@pytest.mark.parametrize("entries, behind", [
    ([(LOW, BEHIND_S + 1)], False),                         # a context talkgroup's clip: kept up to 15 min by design
    ([(CONTEXT_FALLBACK, 1000.0), (CONTEXT, 500.0)], False),
    ([(RERUN, 5000.0)], False),
    ([(LIVE, BEHIND_S - 1), (LOW, 800.0)], False),
    ([(LIVE, BEHIND_S + 1)], True),
    ([(HIGH, BEHIND_S + 1)], True),
    ([(URGENT, BEHIND_S + 5), (LOW, 10.0)], True),
])
def test_whisper_is_behind_only_on_live_traffic(entries, behind):
    link = SimpleNamespace(app=SimpleNamespace(stt=SimpleNamespace(_q=_q(*entries))))
    assert StationLink._whisper_behind(link) is behind


def test_whisper_behind_is_never_a_guess_that_holds_anything():
    for q in (None, SimpleNamespace(clock=lambda: 1.0, _cv=threading.Lock(), _items=[["x"]]),
              SimpleNamespace(clock=lambda: 1 / 0, _cv=threading.Lock(), _items=[])):
        assert StationLink._whisper_behind(SimpleNamespace(app=SimpleNamespace(stt=SimpleNamespace(_q=q)))) is False


def test_a_live_backlog_holds_normal_and_low_never_pre_or_fire():
    behind = threading.Event()
    behind.set()
    made = []
    enc = sttinput.Encoder(lambda d, data, meta, prio: made.append((d["id"], prio)), log=lambda m: None,
                           max_items=5, max_s=100.0, held=behind.is_set, mode="thread")
    try:
        a = voice(0.5)
        for tid, prio in (("ctx-1", LANE_LOW), ("Ops-1", NORMAL), ("ctx-2", LANE_LOW)):
            assert enc.submit(_tx(tid, a), a, prio)
        assert enc.submit(_tx("FG1-1", a), a, FIRE) and enc.submit(_tx("South2-1", a), a, PRE)
        assert wait(lambda: len(made) == 2, 10)                             # PRE and FIRE: at once, whisper behind or not
        assert made == [("South2-1", PRE), ("FG1-1", FIRE)]
        time.sleep(1.5)
        assert len(made) == 2 and enc.holding and enc.stats()["queued"] == 3
        assert enc.submit(_tx("South2-2", a), a, PRE)                       # a page in the middle of a hold: at once
        assert wait(lambda: len(made) == 3, 5) and made[-1] == ("South2-2", PRE)
        behind.clear()                                                      # whisper caught up: the rest, highest first
        assert wait(lambda: len(made) == 6, 10)
        assert made[3:] == [("Ops-1", NORMAL), ("ctx-1", LANE_LOW), ("ctx-2", LANE_LOW)]
        assert not enc.holding and enc.held_s >= 1.0
    finally:
        enc.stop()


def test_a_hold_lasts_at_most_its_cap(monkeypatch):
    monkeypatch.setattr(sttinput, "HOLD_MAX_S", 1.0)
    made = []
    enc = sttinput.Encoder(lambda d, data, meta, prio: made.append(d["id"]), log=lambda m: None,
                           held=lambda: True, mode="thread")                # behind all evening
    try:
        a = voice(0.5)
        t0 = time.monotonic()
        assert enc.submit(_tx("Ops-1", a), a, NORMAL)
        assert wait(lambda: made == ["Ops-1"], 10)
        assert 0.9 <= time.monotonic() - t0 < 5.0                           # held its 1 s, then let go
        assert enc.submit(_tx("Ops-2", a), a, NORMAL)                      # (still behind: the cap goes on)
        assert wait(lambda: made == ["Ops-1", "Ops-2"], 3)
    finally:
        enc.stop()


@pytest.fixture
def station(tmp_path):
    st = offline_station(tmp_path)
    yield st
    st.link.stop()


def test_the_station_holds_only_its_normal_and_low_inputs_while_whisper_is_behind(station, monkeypatch):
    from sizeup import app as app_mod
    st = station
    monkeypatch.setattr(app_mod, "AIRBAND_FIRST", {"HPN ATC"})
    behind = [True]
    st.link.stt_encoder.held = lambda: behind[0]
    fire = _transmission(st, "FG1-1790000000300", "FG1", voice(1.0, seed=5), True, eng="aws")
    air = _transmission(st, "HPNATC-1790000000301", "HPN ATC", voice(1.0, seed=6), False, eng="aws")
    assert wait(lambda: stt_rows(st, fire.id), 10)                          # fire traffic: never held
    time.sleep(1.0)
    assert stt_rows(st, air.id) == [] and st.link.stt_stats()["held"] is True
    behind[0] = False
    assert wait(lambda: stt_rows(st, air.id), 10)
    assert st.link.stt_stats()["held"] is False and st.link.stt_stats()["encoder"]
    assert "dropped_fire" in st.link.stt_stats()


# -- the unit (#709) --------------------------------------------------------------------------------------------------


def test_the_encode_service_unit_runs_below_whisper_with_the_station_and_the_installer_runs_it():
    from pathlib import Path
    station = Path(__file__).resolve().parent.parent / "deploy" / "station"
    unit = (station / "systemd" / "sizeup-stt-encode.service").read_text()
    for want in ("User=sizeup", "Group=sizeup", "CPUWeight=10", "Nice=19", "NoNewPrivileges=yes",
                 "RuntimeDirectory=sizeup-stt-encode", "RuntimeDirectoryMode=0700", "PartOf=sizeup-station.service",
                 "After=sizeup-station.service", "WantedBy=sizeup-station.service", "Restart=always",
                 f"-m sizeup.cloud.sttencode --serve {sttencode.SOCKET}"):
        assert want in unit, want
    settings = [ln for ln in unit.splitlines() if ln and not ln.startswith("#")]
    assert not [ln for ln in settings if ln.startswith(("User=root", "AmbientCapabilities", "CapabilityBoundingSet"))]
    assert "CPUWeight=100" in (station / "systemd" / "sizeup-whisper.service").read_text()   # (what it stays under)
    assert sttencode.SOCKET == "/run/sizeup-stt-encode/encode.sock"
    sh = (station / "install-linux.sh").read_text()
    assert "sizeup-trunk-recorder.service sizeup-stt-encode.service; do" in sh                # installed
    assert "systemctl enable --quiet sizeup-stt-encode.service" in sh                          # enabled
    restart = sh.index("systemctl restart sizeup-stt-encode.service")
    assert sh.index("systemctl restart sizeup-station.service") < restart                     # after the station
    assert "/etc/systemd/system/sizeup-stt-encode.service \\" in sh                          # verified
