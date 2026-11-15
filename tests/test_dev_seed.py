"""tools/dev_seed.py: the invented calls a local instance is filled with (docs/development.md), against a real hub; and
the station token never leaving this machine (no proxy, no redirect, no other host)."""

import importlib.util
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad import dispatcher
from tests.test_cloud import TOKEN, HubRun, hub_cfg

ROOT = Path(__file__).resolve().parent.parent


def load():
    """tools/dev_seed.py, loaded now (its opener is built at import, under the environment of the moment)."""
    spec = importlib.util.spec_from_file_location("dev_seed", ROOT / "tools" / "dev_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dev_seed = load()


def test_a_local_hub_gets_the_calls_their_units_and_their_radio_traffic(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    hub = HubRun(hub_cfg(tmp_path))
    try:
        assert dev_seed.main(["--base", hub.url, "--token", TOKEN]) == 0
        incs = {i["address"]: i for i in hub.app.cad.state()["incidents"]}
        assert {a: i["status"] for a, i in incs.items()} == {
            "100 Example Ave": "active", "25 Sample St": "closed", "Example Ave / Sample St": "pending"}
        rows = hub.get("/api/cad/comms?type=radio")["rows"]
        assert len(rows) == sum(len(c["radio"]) for c in dev_seed.CALLS)
        on = {r["text"]: {e["address"] for e in r["events"]} for r in rows}
        for c in dev_seed.CALLS:                  # every transmission on its own call, none on another
            for _, text in c["radio"]:
                assert on[text] == {c["address"]}, text
        assert "radio traffic: 4 transmission(s)" in capsys.readouterr().out
    finally:
        hub.close()


@pytest.mark.parametrize("base", ["https://sizeup.example.org", "http://10.0.0.5:8090", "ftp://127.0.0.1:8090",
                                  "http://user:secret@127.0.0.1:8090", "http://127.0.0.1@sizeup.example.org"])
def test_only_this_machine(base, capsys):
    assert dev_seed.main(["--base", base, "--token", TOKEN]) == 2
    assert "not http(s) to this machine" in capsys.readouterr().err


class Listener:
    """A local TCP listener that only counts who connects (a stand-in proxy, or a redirect's target)."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.got: list[bytes] = []
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            c.settimeout(1)
            try:
                self.got.append(c.recv(4096))
            except OSError:
                self.got.append(b"")
            c.close()

    def close(self):
        self._stop.set()
        self.sock.close()


def answering(code: int, body: bytes = b"{}", headers: dict | None = None):
    """A local HTTP server answering every request with code (and a count of what it was asked)."""
    asked = []

    class H(BaseHTTPRequestHandler):
        def _any(self):
            asked.append((self.command, self.path, self.headers.get("Authorization")))
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(code)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _any

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", asked


def test_the_environments_proxy_is_never_used(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dispatcher, "tts", lambda text, voice="", rate_wpm=0: np.full(8000, 0.1, np.float32))
    proxy = Listener()
    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, f"http://127.0.0.1:{proxy.port}")
    for k in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    seed = load()                                 # imported with the proxy in the environment
    hub = HubRun(hub_cfg(tmp_path))
    try:
        assert seed.main(["--base", hub.url, "--token", TOKEN]) == 0
        assert "radio traffic: 4 transmission(s)" in capsys.readouterr().out
        assert proxy.got == []                    # straight to the hub: the proxy saw nothing, the token least of all
    finally:
        hub.close()
        proxy.close()


def test_a_redirect_is_not_followed_and_the_token_goes_nowhere_else(capsys):
    target = Listener()
    srv, base, asked = answering(307, b"", {"Location": f"http://127.0.0.1:{target.port}/elsewhere"})
    try:
        with pytest.raises(SystemExit) as e:
            dev_seed.main(["--base", base, "--token", TOKEN])
        assert "redirect" in str(e.value) and "not followed" in str(e.value)
        assert len(asked) == 1 and target.got == []
    finally:
        srv.shutdown()
        target.close()


def test_a_station_is_told_to_point_at_the_hub():
    body = b'{"error": "this is the station: the CAD is on the hub (http://127.0.0.1:8090)"}'
    srv, base, _ = answering(409, body)
    try:
        with pytest.raises(SystemExit) as e:
            dev_seed.main(["--base", base])
        assert "point --base at the hub" in str(e.value)
    finally:
        srv.shutdown()


def test_nothing_listening_is_one_line():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(SystemExit) as e:
        dev_seed.main(["--base", f"http://127.0.0.1:{port}"])
    assert "can't reach it" in str(e.value) and "\n" not in str(e.value)
