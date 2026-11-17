"""Kept-alive HTTP(S) GETs (#446): a few idle connections kept per host (one TLS context for them all), so the camera
DVR's chunklists and segments (several a second, from a handful of origins) don't each pay a new TCP connection and
TLS handshake. Measured locally over TLS: ~1.1 ms of CPU per 150 KB segment through urllib, ~0.3 ms kept alive.
(urllib's own opener already keeps one context for the process: building one loads the CA store, ~5 ms.)

get() keeps sizeup.roads.http_get's contract: (status, headers with lower-case names, body); gzip asked for and undone;
a 304 comes back as (304, headers, b""); 4xx/5xx raise HTTPError; a body over limit, or shorter than its
Content-Length, raises ValueError; network errors are URLError / OSError. The same User-Agent and Accept. A redirect,
a proxy in the environment or another scheme: handed to http_get (urllib) as it is. A kept connection the server has
closed meanwhile (it shows as a reset or an empty answer to the next request) is asked again once on a new one.
"""

from __future__ import annotations

import http.client
import ssl
import threading
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import getproxies

from sizeup import USER_AGENT as UA

PER_HOST = 8          # idle connections kept per host (the DVR's workers: 8)
IDLE_S = 30.0         # an idle connection older than this is closed, not used (servers drop theirs after 60 s or so)
ACCEPT = "application/json, image/*;q=0.9, */*;q=0.5"
STALE = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
         http.client.BadStatusLine, ssl.SSLEOFError, ssl.SSLZeroReturnError)

_ctx: ssl.SSLContext | None = None
_ctx_lock = threading.Lock()


def context() -> ssl.SSLContext:
    """The process's one client TLS context (what urllib makes for each request: the system's CAs, http/1.1)."""
    global _ctx
    if _ctx is None:
        with _ctx_lock:
            if _ctx is None:
                ctx = ssl.create_default_context()
                ctx.set_alpn_protocols(["http/1.1"])
                _ctx = ctx
    return _ctx


class Pool:
    def __init__(self, per_host: int = PER_HOST, idle_s: float = IDLE_S, clock: Callable[[], float] = time.monotonic,
                 fallback: Callable | None = None):
        self.per_host, self.idle_s, self.clock = int(per_host), float(idle_s), clock
        self.fallback = fallback
        self._idle: dict[tuple[str, str, int], list[tuple[float, http.client.HTTPConnection]]] = {}
        self._lock = threading.Lock()
        self.opened = 0                               # connections made (the tests, the benchmark)

    def _take(self, key: tuple[str, str, int], timeout: float) -> tuple[http.client.HTTPConnection, bool]:
        """(an idle connection to that host, True), else (a new one, False)."""
        now, stale = self.clock(), []
        with self._lock:
            idle = self._idle.get(key) or []
            conn = None
            while idle:
                at, c = idle.pop()
                if now - at <= self.idle_s:
                    conn = c
                    break
                stale.append(c)
            if conn is None:
                self.opened += 1
        for c in stale:
            c.close()
        if conn is not None:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            return conn, True
        scheme, host, port = key
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=context()), False
        return http.client.HTTPConnection(host, port, timeout=timeout), False

    def _give(self, key: tuple[str, str, int], conn: http.client.HTTPConnection) -> None:
        with self._lock:
            idle = self._idle.setdefault(key, [])
            if len(idle) < self.per_host:
                idle.append((self.clock(), conn))
                return
        conn.close()

    def close(self) -> None:
        with self._lock:
            conns = [c for idle in self._idle.values() for _, c in idle]
            self._idle.clear()
        for c in conns:
            c.close()

    def _urllib(self, url: str, timeout: float, limit: int, headers: dict | None):
        if self.fallback is not None:
            return self.fallback(url, timeout, limit, headers)
        from sizeup.roads import http_get
        return http_get(url, timeout, limit, headers)

    def get(self, url: str, timeout: float, limit: int, headers: dict | None = None) -> tuple[int, dict, bytes]:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname or getproxies().get(scheme):
            return self._urllib(url, timeout, limit, headers)
        key = (scheme, parts.hostname, parts.port or (443 if scheme == "https" else 80))
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        hdrs = {"User-Agent": UA, "Accept": ACCEPT, "Accept-Encoding": "gzip", **(headers or {})}
        for attempt in (1, 2):
            conn, reused = self._take(key, timeout)
            try:
                conn.request("GET", path, headers=hdrs)
                r = conn.getresponse()
            except STALE as e:
                conn.close()
                if reused and attempt == 1:
                    continue                          # the server had closed it: once more, on a new connection
                raise URLError(e) from e
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                raise URLError(e) from e
            try:
                return self._answer(url, conn, r, key, limit)
            except BaseException:
                conn.close()
                raise
        raise URLError("no answer")                   # pragma: no cover - the loop returns or raises

    def _answer(self, url: str, conn: http.client.HTTPConnection, r: http.client.HTTPResponse,
                key: tuple[str, str, int], limit: int) -> tuple[int, dict, bytes]:
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        if r.status in (301, 302, 303, 307, 308):
            conn.close()
            return self._urllib(url, conn.timeout, limit, None)
        if r.status >= 400:
            raise HTTPError(url, r.status, r.reason, r.headers, None)    # (the connection closed: get)
        try:
            body = r.read(limit + 1)
        except http.client.HTTPException as e:       # a chunked body cut short
            raise ValueError(f"cut short ({type(e).__name__})") from e
        if r.status == 304:
            self._done(key, conn, r)
            return 304, hdrs, b""
        if len(body) > limit:
            raise ValueError(f"more than {limit // (1024 * 1024)} MB")
        # read(n) hands back what came when the server closes early (#276: a segment cut short is never kept)
        want = hdrs.get("content-length", "").strip()
        if want.isdigit() and len(body) < int(want):
            raise ValueError(f"cut short: {len(body)} of {want} bytes")
        if hdrs.get("content-encoding", "").lower() == "gzip":
            from sizeup.roads import _gunzip
            body = _gunzip(body, limit)
        self._done(key, conn, r)
        return r.status, hdrs, body

    def _done(self, key: tuple[str, str, int], conn: http.client.HTTPConnection, r: http.client.HTTPResponse) -> None:
        """Kept for the next request when the answer was read to its end and the server keeps the connection open."""
        if r.isclosed() and not r.will_close and conn.sock is not None:
            self._give(key, conn)
        else:
            conn.close()


POOL = Pool()


def get(url: str, timeout: float, limit: int, headers: dict | None = None) -> tuple[int, dict, bytes]:
    return POOL.get(url, timeout, limit, headers)
