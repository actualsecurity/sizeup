"""The station's HTTPS client to the hub: one kept-alive connection per thread (a new TLS handshake
through Cloudflare per request would add ~100-200 ms to every "talking now" update), the bearer
token on every request and, when set, the Cloudflare Access service-token headers."""

from __future__ import annotations

import http.client
import json
import ssl
from urllib.parse import urlsplit


class HubError(RuntimeError):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status       # 0 = never reached the hub (network, TLS, timeout)


class HubClient:
    def __init__(self, base_url: str, token: str, access_id: str = "", access_secret: str = "",
                 timeout: float = 15.0, user_agent: str = ""):
        u = urlsplit(base_url)
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname or "", u.port or (443 if self.https else 80)
        self.prefix = (u.path or "").rstrip("/")
        self.timeout = timeout
        from sizeup import USER_AGENT
        self.headers = {"Authorization": f"Bearer {token}", "User-Agent": user_agent or USER_AGENT + " station", "Accept": "application/json"}
        if access_id and access_secret:
            self.headers.update({"CF-Access-Client-Id": access_id, "CF-Access-Client-Secret": access_secret})
        self._conn: http.client.HTTPConnection | None = None
        self._conn_timeout = 0.0

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        if self._conn is None or self._conn_timeout != timeout:
            self.close()
            if self.https:
                self._conn = http.client.HTTPSConnection(self.host, self.port, timeout=timeout,
                                                         context=ssl.create_default_context())
            else:
                self._conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
            self._conn_timeout = timeout
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = None

    def request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        """JSON in, JSON out. HubError on anything but a 2xx JSON answer (the connection is then dropped)."""
        data = (json.dumps(body, separators=(",", ":"), default=lambda o: o.item() if hasattr(o, "item") else str(o)).encode()
                if body is not None else None)
        headers = dict(self.headers, **({"Content-Type": "application/json"} if data is not None else {}))
        for attempt in (0, 1):     # a kept-alive connection the hub (or Cloudflare) closed: one quiet retry
            conn = self._connect(timeout or self.timeout)
            try:
                conn.request(method, self.prefix + path, body=data, headers=headers)
                r = conn.getresponse()
                raw = r.read()
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError) as e:
                self.close()
                if attempt == 0:
                    continue
                raise HubError(f"hub closed the connection: {e}") from e
            except (OSError, http.client.HTTPException) as e:
                self.close()
                raise HubError(f"can't reach the hub: {type(e).__name__}: {e}") from e
            if r.getheader("Connection", "").lower() == "close":
                self.close()
            if not 200 <= r.status < 300:
                self.close()
                try:
                    msg = json.loads(raw).get("error") or raw[:200].decode(errors="replace")
                except (ValueError, AttributeError):
                    msg = raw[:200].decode(errors="replace").strip() or r.reason
                # Cloudflare Access answers a missing/expired service token with a login page (302/403 HTML)
                raise HubError(f"hub said {r.status}: {msg}", r.status)
            try:
                return json.loads(raw or b"{}")
            except ValueError as e:
                self.close()
                raise HubError(f"hub sent something that isn't JSON ({r.getheader('Content-Type')}): "
                               "is Cloudflare Access letting the station through?", r.status) from e
        raise HubError("unreachable")
