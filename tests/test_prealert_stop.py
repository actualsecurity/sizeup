"""#546: stopping the phone pre-alert returns at once, with its streams blocked mid-read.

A deploy's drain (HubApp.stop) stopped the pre-alert first, and NtfyStream.stop() closed the response while the reader
thread sat in resp.readline(): BufferedReader.close() waits for the lock the blocked read holds, i.e. for ntfy's next
keepalive (~45 s), so the old app was killed at the 30 s drain limit before its final sync. Here a fake ntfy on
127.0.0.1 sends its headers and "open", then nothing (or a keepalive every 45 s, as ntfy does): stop() must return
within a second, the reader end without an error or a reconnect, and the hub's stop reach its final sync.
"""

from __future__ import annotations

import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from urllib.request import Request, urlopen

import pytest

from sizeup import prealert as prealert_mod
from sizeup.cloud import hub as hub_mod
from sizeup.netcut import cut, socket_of
from sizeup.prealert import NoFeed, NtfyStream, PreAlerts
from tests.test_cad import make_cad
from tests.test_cloud import HubRun, hub_cfg

RING, STATUS = "example-ring-topic", "example-status-topic"
FAST = 1.0                      # "returns at once": well within this


class QuietNtfy:
    """ntfy's JSON stream, stuck: headers (chunked, as ntfy sends them), "open", then nothing, or a keepalive every
    keepalive_s. Counts the connections (a reconnect after stop() would be a second one)."""

    def __init__(self, keepalive_s: float | None = None, tls: ssl.SSLContext | None = None):
        self.keepalive_s, self.tls = keepalive_s, tls
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.url = f"{'https' if tls else 'http'}://127.0.0.1:{self.port}"
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.paths: list[str] = []
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self.closed.is_set():
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket) -> None:
        try:
            if self.tls is not None:
                c = self.tls.wrap_socket(c, server_side=True)
            with self.lock:
                self.conns.append(c)
            head = b""
            while b"\r\n\r\n" not in head:
                b = c.recv(4096)
                if not b:
                    return
                head += b
            with self.lock:
                self.paths.append(head.split(b" ", 2)[1].decode())
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson; charset=utf-8\r\n"
                      b"Transfer-Encoding: chunked\r\n\r\n")
            self._send(c, {"id": "op3n", "time": int(time.time()), "event": "open"})
            while not self.closed.wait(self.keepalive_s):
                self._send(c, {"id": "k33p", "time": int(time.time()), "event": "keepalive"})
        except OSError:
            return

    @staticmethod
    def _send(c, obj) -> None:
        body = (json.dumps(obj) + "\n").encode()
        c.sendall(b"%x\r\n%s\r\n" % (len(body), body))

    def connections(self) -> int:
        with self.lock:
            return len(self.paths)

    def close(self) -> None:
        self.closed.set()
        try:
            self.srv.close()
        except OSError:
            pass
        with self.lock:
            for c in self.conns:
                try:
                    c.close()
                except OSError:
                    pass


@pytest.fixture(params=[None, 45.0], ids=["silent", "keepalive-45s"])
def quiet(request):
    f = QuietNtfy(keepalive_s=request.param)
    yield f
    f.close()


def wait(cond, s=5.0) -> bool:
    end = time.monotonic() + s
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return bool(cond())


def timed(fn) -> float:
    t0 = time.monotonic()
    fn()
    return time.monotonic() - t0


def connected_stream(server: str, logs: list) -> NtfyStream:
    s = NtfyStream(server, RING, lambda m: None, logs.append, label="ring").start()
    assert wait(lambda: s.connected), s.last_error
    time.sleep(0.1)                                           # the reader is in readline() now, waiting
    return s


def test_stop_returns_at_once_with_the_read_blocked_and_the_reader_ends_quietly(quiet):
    logs = []
    s = connected_stream(quiet.url, logs)
    took = timed(s.stop)
    assert took < FAST, f"stop() took {took:.1f} s"
    assert s.join(FAST), "the reader thread is still running"
    time.sleep(0.3)                                           # past the first backoff (1 s in production, 0 needed)
    assert quiet.connections() == 1                           # no reconnect
    assert logs == [] and s.last_error == "" and not s.connected     # no error, nothing logged


def test_stop_is_idempotent_and_safe_before_start_and_after_the_end(quiet):
    never = NtfyStream(quiet.url, RING, lambda m: None, lambda m: None)
    assert timed(never.stop) < FAST and timed(never.stop) < FAST and never.join(0)
    logs = []
    s = connected_stream(quiet.url, logs)
    for _ in range(3):
        assert timed(s.stop) < FAST
    assert s.join(FAST) and timed(s.stop) < FAST and logs == []
    assert quiet.connections() == 1


def test_a_stop_while_connecting_never_reads_or_reconnects(monkeypatch):
    """stop() lands between the connect and the first read: the reader sees it and ends; no read waits on the socket."""
    q = QuietNtfy()
    gate, go = threading.Event(), threading.Event()
    real = prealert_mod._get

    def slow_get(url, timeout):
        r = real(url, timeout)
        gate.set()
        go.wait(5)
        return r
    monkeypatch.setattr(prealert_mod, "_get", slow_get)
    logs = []
    try:
        s = NtfyStream(q.url, RING, lambda m: None, logs.append).start()
        assert gate.wait(5)
        assert timed(s.stop) < FAST
        go.set()
        assert s.join(FAST) and logs == [] and q.connections() == 1 and not s.connected
    finally:
        q.close()


def _self_signed(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("no openssl to make a test certificate")
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
                        "-days", "1", "-subj", "/CN=127.0.0.1"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"openssl: {r.stderr[-200:]}")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    return ctx


def test_stop_under_tls_too(tmp_path, monkeypatch):
    """ntfy.sh is HTTPS: the reader blocks in an SSL read. The plain socket's shutdown wakes it the same way."""
    q = QuietNtfy(tls=_self_signed(tmp_path))
    unverified = ssl._create_unverified_context()            # (the test's own certificate)
    monkeypatch.setattr(prealert_mod, "_get", lambda url, timeout: urlopen(
        Request(url, method="GET", headers={"Accept": "application/x-ndjson"}), timeout=timeout, context=unverified))
    logs = []
    try:
        s = connected_stream(q.url, logs)
        assert isinstance(s._sock, ssl.SSLSocket)
        took = timed(s.stop)
        assert took < FAST, f"stop() took {took:.1f} s"
        assert s.join(FAST) and logs == [] and s.last_error == "" and q.connections() == 1
    finally:
        q.close()


def test_the_old_close_did_block_which_is_why_it_is_gone(quiet):
    """The cause, shown: resp.close() from another thread waits on the blocked readline(). (Bounded here: a thread.)"""
    if quiet.keepalive_s is not None:
        pytest.skip("one case is enough")
    resp = urlopen(Request(quiet.url + f"/{RING}/json", method="GET"), timeout=30)
    def read() -> None:
        try:
            resp.readline()
            resp.readline()
        except Exception:  # noqa: BLE001 - cut under it
            pass
    sock = socket_of(resp)                                    # (resp.close() drops resp.fp before it blocks)
    assert sock is not None
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    time.sleep(0.2)                                           # "open" read; blocked on the next line
    closer = threading.Thread(target=resp.close, daemon=True)
    closer.start()
    closer.join(1.0)
    blocked = closer.is_alive()
    cut(sock)                                                 # and the fix: the read wakes, the close completes
    closer.join(FAST)
    reader.join(FAST)
    assert blocked and not closer.is_alive() and not reader.is_alive()


def test_cut_is_harmless_on_nothing_and_on_a_closed_socket():
    cut(None)
    s = socket.socket()
    s.close()
    cut(s)
    assert socket_of(object()) is None


# -- PreAlerts ----------------------------------------------------------------------------------------------


def _prealerts(tmp_path, server, log, **kw) -> PreAlerts:
    return PreAlerts(make_cad(tmp_path), server, RING, STATUS, log=log, **kw)


def test_prealerts_stop_returns_at_once_with_both_streams_blocked(tmp_path, quiet):
    logs = []
    pa = _prealerts(tmp_path, quiet.url, logs.append).start()
    assert wait(lambda: pa.ring_stream.connected and pa.status_stream.connected)
    time.sleep(0.1)
    took = timed(pa.stop)
    assert took < FAST, f"PreAlerts.stop() took {took:.1f} s"
    assert pa.ring_stream.join(FAST) and pa.status_stream.join(FAST) and not pa.running
    time.sleep(0.3)
    assert quiet.connections() == 2                           # one each, no reconnect
    assert [x for x in logs if "subscribed" not in x] == []
    assert timed(pa.stop) < FAST                              # again: nothing to do


def test_prealerts_stop_never_holds_its_lock_while_the_streams_stop(tmp_path):
    """The lock (the ring handler, the topic check) is free while each stream's stop() runs."""
    pa = _prealerts(tmp_path, "http://127.0.0.1:9", lambda m: None)
    free = []

    def stop():
        got = []

        def other() -> None:
            ok = pa._lock.acquire(timeout=1.0)
            got.append(ok)
            if ok:
                pa._lock.release()
        t = threading.Thread(target=other)
        t.start()
        t.join(2)
        free.append(got == [True])
    pa.ring_stream.stop = stop
    pa.status_stream.stop = stop
    pa.stop()
    assert free == [True, True]


def test_prealerts_with_no_topic_stop_at_once(tmp_path):
    pa = PreAlerts(make_cad(tmp_path), "http://127.0.0.1:9", log=lambda m: None).start()
    assert isinstance(pa.ring_stream, NoFeed) and isinstance(pa.status_stream, NoFeed)
    assert timed(pa.stop) < FAST and timed(pa.stop) < FAST and not pa.running


@pytest.mark.skipif(not hasattr(prealert_mod, "OffFeed"), reason="the outbound-off feed (OffFeed) is not on this branch")
def test_prealerts_with_outbound_off_stop_at_once(tmp_path):
    """Outbound off (a hub started by hand): OffFeed stands for each topic, never a request; stop() is as quick."""
    from sizeup import outbound
    outbound.arm("hub", env={})
    try:
        logs = []
        pa = PreAlerts(make_cad(tmp_path), "https://ntfy.example", log=logs.append,
                       topics=lambda: (RING, STATUS)).start()
        assert isinstance(pa.ring_stream, prealert_mod.OffFeed) and isinstance(pa.status_stream, prealert_mod.OffFeed)
        n = len(logs)
        assert timed(pa.stop) < FAST and timed(pa.stop) < FAST and not pa.running
        time.sleep(0.2)
        assert logs[n:] == []
    finally:
        outbound.disarm()


# -- the hub's stop: the drain's share of a deploy ------------------------------------------------------------


def test_the_hub_stop_is_quick_with_both_pre_alert_streams_connected(tmp_path, quiet):
    hub = HubRun(hub_cfg(tmp_path, prealert_enabled=True, prealert_ring_topic=RING, prealert_status_topic=STATUS,
                         prealert_server=quiet.url))
    pa = hub.app.prealert
    assert wait(lambda: pa.ring_stream.connected and pa.status_stream.connected)
    time.sleep(0.1)
    lines = []
    hub.app.hub.log = lines.append
    took = timed(hub.close)
    timing = next(x for x in lines if x.startswith("stop timing:"))
    layers = float(timing.split("layers ", 1)[1].split(" s", 1)[0])
    assert layers < 2.0, timing                                # was 19-27 s in production
    assert "sync" in timing and took < 5.0, (took, timing)
    assert not [x for x in lines if "still stopping" in x]
    assert quiet.connections() == 2


def test_a_slow_stop_never_keeps_the_hub_from_its_final_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_mod, "STOP_LAYERS_S", 0.3)
    hub = HubRun(hub_cfg(tmp_path))
    release = threading.Event()
    synced = []
    hub.app.stop_layers = lambda: release.wait(10)             # a provider that hangs
    real_sync = hub.app.sync
    hub.app.sync = lambda: (synced.append(time.monotonic()), real_sync())
    lines = []
    hub.app.hub.log = lines.append
    try:
        took = timed(hub.close)
        assert took < 3.0 and synced, (took, lines)
        assert "hub: stop: layers still stopping after 0.3 s: going on without it" in lines
        assert any(x.startswith("stop timing:") and "sync" in x for x in lines)
    finally:
        release.set()


def test_one_stop_that_raises_is_logged_and_the_rest_still_run():
    ran, logs = [], []

    def boom():
        raise RuntimeError("invented")
    late = hub_mod._bounded([("a", lambda: ran.append("a")), ("b", boom), ("c", lambda: ran.append("c"))], 2.0,
                            logs.append)
    assert late == "" and ran == ["a", "c"] and logs == ["hub: stop: b: RuntimeError: invented"]
