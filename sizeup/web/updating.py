"""The "update in progress" answer on the hub's port while no app serves there (GitHub #247, #264).

Two places answer it: the old task's supervisor after its app has drained (sizeup/handoff.py), and the new task's
app from the moment it may take over until the CAD is built (sizeup/app.py run_gated: the port listens at once,
the Cloudflare connector starts beside it, and the real server takes the same socket over). Every answer is a 503
with Retry-After; /healthz answers 503 too, so nothing mistakes it for the app.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPDATING_JSON = json.dumps({"error": "the hub is updating: back in a few seconds", "updating": True}).encode()
# It reloads itself with location.reload(), which keeps the whole URL, the hash included (#316): a plain
# <meta http-equiv=refresh> goes to the page's URL, and when that has a hash (/cad#settings/radios) the browser takes it
# as a jump within the page and never asks the server again. The meta refresh stays for a browser without script.
UPDATING_HTML = (b"<!doctype html><meta charset=utf-8><noscript><meta http-equiv=refresh content=3></noscript>"
                 b"<title>Updating</title><script>setTimeout(function () { location.reload(); }, 3000);</script>"
                 b"<body style=\"font:16px system-ui;margin:3em;color:#333\"><p>Reconnecting&hellip; (update in progress)"
                 b"</p><p>This page reloads by itself.</p>")
STARTING_HEALTH = json.dumps({"ok": False, "serving": False, "updating": True}).encode()
RETRY_AFTER = "2"


class Updating(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a) -> None:
        pass

    def _answer(self) -> None:
        if self.path.split("?", 1)[0] == "/healthz":
            html, body = False, STARTING_HEALTH
        else:
            html = "text/html" in (self.headers.get("Accept") or "") and not self.path.startswith("/api/")
            body = UPDATING_HTML if html else UPDATING_JSON
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json")
        self.send_header("Retry-After", RETRY_AFTER)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _answer


def serve_updating(host: str, port: int) -> ThreadingHTTPServer:
    """The updating answer on (host, port), in a thread. OSError if the port is taken."""
    srv = ThreadingHTTPServer((host, port), Updating)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, name=f"updating-{port}",
                     daemon=True).start()
    return srv
