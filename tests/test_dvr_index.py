"""The camera DVR's index kept as it changes (#446, sizeup/dvr.py _Segs): each camera's byte total and its segments in
time order, so the hub's DVR thread never walks every kept segment (19 cameras x 12 h: ~128,000) several times a second;
the disk's free space read at most every FREE_EVERY_S; the camera fetches over kept-alive connections
(sizeup/httpkeep.py).

The retention is the same as before: a property test runs random sequences of writes, rewrites, out-of-order arrivals
(a scan), removals and evictions through the Dvr and through the old whole-index eviction, and compares what is kept.
Segment sizes and durations have the shape measured on the 511NY cameras (about 200 kbit/s, 2-6 s segments). No network:
the Thruway's camera fixtures with a moving Wowza-like chunklist (tests/test_hlsseg.py Origin), and a server on 127.0.0.1.
"""

from __future__ import annotations

import gzip
import random
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError

import pytest

from sizeup import USER_AGENT, httpkeep
from sizeup import dvr as dvr_mod
from sizeup.dvr import Dvr, _Segs
from tests.test_dvr import CAM, setup, step


class _Roads:
    def _changed(self):
        pass


def _old_room(cams: dict, need: int, max_bytes: int, floor: int, free) -> tuple[bool, list]:
    """The eviction before #446, on plain dicts: the whole index summed and searched for the oldest each time."""
    gone = []
    while True:
        total = sum(s[2] for segs in cams.values() for s in segs.values())
        if total + need <= max_bytes and free(total) - need >= floor:
            return True, gone
        every = [(s[0], cid, key) for cid, segs in cams.items() for key, s in segs.items()]
        if not every:
            return False, gone
        _, cid, key = min(every)
        cams[cid].pop(key)
        gone.append((cid, key))


def _old_evict(cams: dict, now: float, window: float, max_bytes: int, floor: int, free) -> None:
    for cid, segs in cams.items():
        for key in [k for k, s in segs.items() if s[0] < now - window]:
            segs.pop(key)
    _old_room(cams, 0, max_bytes, floor, free)


def _kept(d: Dvr) -> dict:
    return {c: dict(v["segs"]) for c, v in d._cams.items() if v["segs"]}


@pytest.mark.parametrize("seed", range(12))
def test_the_eviction_keeps_exactly_what_the_whole_index_eviction_kept(tmp_path, seed, monkeypatch):
    monkeypatch.setattr(dvr_mod, "FREE_SLACK", 0)          # the free space taken from the last reading where it can be
    rnd = random.Random(seed)
    d = Dvr(_Roads(), tmp_path / "dvr", window_s=600, max_bytes=40 * 150_000, free_floor=0, log=lambda m: None)
    disk = 60 * 150_000                                   # a small disk: its free floor binds too
    d._free = lambda: disk - d.total_bytes()
    ref: dict[str, dict] = {}
    cids = [f"cam{i}" for i in range(rnd.randint(1, 5))]
    nxt = {c: rnd.randint(0, 10 ** 6) for c in cids}
    now = 1_791_915_000.0
    for _ in range(400):
        now += rnd.uniform(0, 6)
        op = rnd.random()
        cid = rnd.choice(cids)
        segs = d._cam(cid)["segs"]
        ref.setdefault(cid, {})
        if op < 0.6:                                      # a new segment, in time (now and then: a tie, or late)
            key = nxt[cid]
            nxt[cid] += rnd.choice((1, 1, 1, 2, 30))
            t0 = round(now - rnd.choice((0, 0, 0, 0, 0, 3, 200)), 3)
            s = (t0, rnd.choice((2.0, 4.0, 6.0)), rnd.randint(60_000, 260_000))
        elif op < 0.7 and ref[cid]:                       # a key written again (a counter that started over)
            key = rnd.choice(list(ref[cid]))
            s = (round(now, 3), 4.0, rnd.randint(60_000, 260_000))
        elif op < 0.8 and ref[cid]:                       # gone from under us (the other writer evicted it)
            key = rnd.choice(list(ref[cid]))
            with d._lock:
                segs.pop(key, None)
            ref[cid].pop(key, None)
            continue
        else:                                             # the window and the cap, the floor moved now and then
            d.free_floor = rnd.choice((0, 0, 10 * 150_000, 30 * 150_000))
            d.max_bytes = rnd.choice((40, 40, 25, 10)) * 150_000
            d.clock = lambda now=now: now
            d.evict()
            _old_evict(ref, now, d.window_s, d.max_bytes, d.free_floor, lambda total: disk - total)
            assert _kept(d) == {c: v for c, v in ref.items() if v}
            assert d.total_bytes() == sum(x[2] for v in ref.values() for x in v.values())
            continue
        with d._lock:
            segs[key] = s
        ref[cid][key] = s
        if rnd.random() < 0.3:                            # room for a write: the same answer, the same ones gone
            need = rnd.randint(60_000, 260_000)
            want = _old_room(ref, need, d.max_bytes, d.free_floor, lambda total: disk - total)[0]
            assert d._room(need) is want
            assert _kept(d) == {c: v for c, v in ref.items() if v}
        assert d._cams[cid]["segs"].bytes == sum(x[2] for x in ref[cid].values())


class _NoWalk(_Segs):
    """An index that may not be walked: the total, the window and the cap must come from its running sums and head."""
    __slots__ = ()

    def _walked(self, *a):
        raise AssertionError("the whole index walked")
    values = items = keys = __iter__ = _walked


def test_the_total_the_window_and_the_cap_never_walk_every_segment(tmp_path):
    d = Dvr(_Roads(), tmp_path / "dvr", window_s=43200, max_bytes=25 * 1024 ** 3, free_floor=0, log=lambda m: None)
    t, rnd = 1_791_915_000.0, random.Random(7)
    for i in range(19):                                    # 19 cameras x 12 h of ~6 s segments
        segs = d._cam(f"cam{i}")["segs"]
        for k in range(7200):
            segs[k] = (t - 43200 + k * 6.0, 6.0, rnd.randint(100_000, 200_000))
        segs.__class__ = _NoWalk
    total = d.total_bytes()
    assert total == sum(dict.__getitem__(d._cams[c]["segs"], k)[2] for c in d._cams for k in range(7200))
    d.clock = lambda: t + 12.0                             # two segments of each camera now past the window
    assert d.evict() > 0 and d.total_bytes() < total
    assert all(len(d._cams[c]["segs"]) == 7198 for c in d._cams)
    d.max_bytes = d.total_bytes() - 1                      # one over the cap: the oldest of all goes
    oldest = min((dict.__getitem__(c["segs"], 2)[0], cid) for cid, c in d._cams.items())[1]
    assert d._room(0) and 2 not in d._cams[oldest]["segs"] and len(d._cams[oldest]["segs"]) == 7197


def test_the_work_per_second_does_not_grow_with_uptime(tmp_path):
    """Hours of a segment in and the oldest out: the time order holds what is kept and no more (what went is dropped
    from its head), so the eviction's walk stays the few segments past the window."""
    d = Dvr(_Roads(), tmp_path / "dvr", window_s=600, max_bytes=10 ** 12, free_floor=0, log=lambda m: None)
    t = 1_791_915_000.0
    segs = d._cam(CAM)["segs"]
    for k in range(150):                                   # a full window, as a restart reads it
        segs[k] = (t - 600 + k * 4.0, 4.0, 150_000)
    for k in range(150, 150 + 3 * 150):                    # three windows more, one segment and one eviction a step
        t += 4.0
        segs[k] = (t, 4.0, 150_000)
        d.clock = lambda: t
        d.evict()
        assert len(segs) <= 151 and len(segs._order) <= len(segs) + 1


def test_the_free_space_is_read_every_30_s_and_at_once_near_the_floor(tmp_path, monkeypatch):
    reads = []
    free = {"n": 50 * 1024 ** 3}

    def usage(p):
        reads.append(p)
        return shutil._ntuple_diskusage(100 * 1024 ** 3, 0, free["n"])
    monkeypatch.setattr(dvr_mod.shutil, "disk_usage", usage)
    r, d, f, clock = setup(tmp_path)
    d.free_floor = 3 * 1024 ** 3
    mono = {"t": 1000.0}
    d.mono = lambda: mono["t"]
    for _ in range(50):                                    # a second's writes and evictions, many times over
        assert d._room(200_000)
    assert len(reads) == 1
    mono["t"] += dvr_mod.FREE_EVERY_S + 1
    assert d._room(200_000) and len(reads) == 2
    free["n"] = d.free_floor + 100 * 1024 ** 2             # near the floor: read every time, never on the old figure
    mono["t"] += dvr_mod.FREE_EVERY_S + 1
    d._room(0)
    n = len(reads)
    assert d._room(50 * 1024 ** 2) and len(reads) == n + 1
    free["n"] = d.free_floor - 1                           # under it: refused at once (nothing recorded to evict)
    assert not d._room(1) and len(reads) == n + 2


def test_a_restart_builds_the_index_from_the_files_as_it_was(tmp_path):
    r, d, f, clock = setup(tmp_path, window_s=60)
    d.pins = {CAM}
    d.tick()
    step(d, f, clock, 6)
    segs = d._cams[CAM]["segs"]
    assert len(segs) > 5
    again = Dvr(r, tmp_path / "dvr", clock=clock, log=lambda m: None, free_floor=0, window_s=60)
    s2 = again._cams[CAM]["segs"]
    assert isinstance(s2, _Segs) and dict(s2) == dict(segs) and s2.bytes == segs.bytes == again.total_bytes()
    assert s2.head() == min((s[0], k) for k, s in segs.items())
    clock.t += 20                                          # the same ones go from both
    d.evict()
    again.evict()
    assert dict(again._cams[CAM]["segs"]) == dict(d._cams[CAM]["segs"]) and again.total_bytes() == d.total_bytes()


def test_an_index_handed_in_as_a_plain_dict_is_taken_over(tmp_path):
    d = Dvr(_Roads(), tmp_path / "dvr", free_floor=0, log=lambda m: None)
    d._cam("a")["segs"] = {5: (100.0, 4.0, 10), 3: (96.0, 4.0, 20)}
    assert d.total_bytes() == 30 and isinstance(d._cams["a"]["segs"], _Segs)
    assert d._cams["a"]["segs"].head() == (96.0, 3)


# -- the fetches -------------------------------------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.seen.append((self.path, self.headers.get("User-Agent"), id(self.connection)))
        body = srv.bodies.get(self.path, b"\x47" * 188 * 1000)
        hdrs = {"Content-Type": "video/MP2T"}
        if self.path == "/missing":
            self.send_response(404)
            body = b"nope"
        elif self.path == "/gz":
            body = gzip.compress(b"#EXTM3U\n")
            hdrs["Content-Encoding"] = "gzip"
            self.send_response(200)
        else:
            self.send_response(200)
        n = len(body) + (1000 if self.path == "/short" else 0)
        self.send_header("Content-Length", str(n))
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
        if self.path == "/short" or srv.drop_after:
            self.close_connection = True                  # gone without a word (an idle keep-alive the server dropped)


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen, srv.bodies, srv.drop_after = [], {}, False
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_the_fetches_keep_one_connection_with_the_same_headers(server):
    srv, base = server
    pool = httpkeep.Pool()
    for i in range(5):
        code, hdrs, body = pool.get(f"{base}/media_w1_{i}.ts", 10.0, 4 << 20)
        assert code == 200 and len(body) == 188_000 and hdrs["content-type"] == "video/MP2T"
    assert pool.opened == 1 and len({c for _, _, c in srv.seen}) == 1
    assert all(ua == USER_AGENT for _, ua, _ in srv.seen)
    assert pool.get(f"{base}/gz", 10.0, 1 << 20)[2] == b"#EXTM3U\n"
    with pytest.raises(HTTPError) as e:
        pool.get(f"{base}/missing", 10.0, 1 << 20)
    assert e.value.code == 404
    with pytest.raises(ValueError, match="cut short"):
        pool.get(f"{base}/short", 10.0, 4 << 20)
    with pytest.raises(ValueError, match="more than"):
        pool.get(f"{base}/big.ts", 10.0, 1000)
    pool.close()
    assert httpkeep.context() is httpkeep.context()        # one TLS context for the process


def test_a_kept_connection_the_server_dropped_is_asked_again_once_on_a_new_one(server):
    srv, base = server
    pool = httpkeep.Pool()
    srv.drop_after = True                                 # every connection closed after its answer, unannounced
    assert pool.get(f"{base}/a.ts", 10.0, 4 << 20)[0] == 200
    assert pool.get(f"{base}/b.ts", 10.0, 4 << 20)[0] == 200
    assert pool.opened == 2 and [p for p, _, _ in srv.seen] == ["/a.ts", "/b.ts"]
    srv.shutdown()                                        # nobody there at all: an error, not a retry loop
    srv.server_close()
    with pytest.raises(URLError):
        httpkeep.Pool().get(f"{base}/c.ts", 2.0, 4 << 20)


def test_the_dvr_records_over_kept_alive_connections_with_the_plain_client(tmp_path, monkeypatch):
    from sizeup import roads
    r, d, f, clock = setup(tmp_path)
    got = []
    monkeypatch.setattr(httpkeep, "get", lambda url, timeout, limit: (got.append(url), f(url, timeout, limit))[1])
    r.fetch = roads.http_get
    d.pins = {CAM}
    assert d.tick() == 3 and got and got[0].endswith("/playlist.m3u8")
