"""A minimal WebSocket client (RFC 6455), standard library only: what the Vessels layer needs for aisstream.io
(sizeup/vessels.py AisStreamFeed). One connection, text or binary messages in, one text message out (the
subscription), pings answered, a close honoured. No extensions (no permessage-deflate), no fragmented sends.

    ws = WebSocket.connect("wss://host/path", headers={"User-Agent": ...}, timeout=30)
    ws.send_text(json.dumps(...))
    for msg in ws.messages(idle_s=5):  # bytes of each whole message (None: idle 5 s); ends on a close; raises on a bad frame
        ...
    ws.close()
"""

from __future__ import annotations

import base64
import hashlib
import os
import select
import socket
import ssl
import struct
from urllib.parse import urlsplit

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 1024 * 1024           # one AIS message is well under 2 KB; anything this large is not ours


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.closed = False

    @classmethod
    def connect(cls, url: str, headers: dict | None = None, timeout: float = 30.0, context=None) -> WebSocket:
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise WebSocketError(f"not a ws:// or wss:// address: {u.scheme}://{u.hostname or ''}")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            sock = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=u.hostname) \
                if u.scheme == "wss" else raw
            key = base64.b64encode(os.urandom(16)).decode()
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            lines = [f"GET {path} HTTP/1.1", f"Host: {u.hostname}" + (f":{u.port}" if u.port else ""),
                     "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
                     "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            ws = cls(sock)
            head = ws._read_until(b"\r\n\r\n", 16384)
            status = head.split(b"\r\n", 1)[0].decode("latin-1")
            if " 101 " not in status + " ":
                raise WebSocketError(f"no upgrade: {status[:80]}")
            want = base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()
            acc = [ln.split(b":", 1)[1].strip().decode("latin-1") for ln in head.split(b"\r\n")[1:]
                   if ln.lower().startswith(b"sec-websocket-accept:")]
            if acc != [want]:
                raise WebSocketError("bad Sec-WebSocket-Accept")
            return ws
        except BaseException:
            raw.close()
            raise

    def _recv(self) -> bytes:
        b = self.sock.recv(65536)
        if not b:
            raise WebSocketError("connection closed")
        return b

    def _read_until(self, mark: bytes, limit: int) -> bytes:
        while mark not in self.buf:
            if len(self.buf) > limit:
                raise WebSocketError("handshake too long")
            self.buf += self._recv()
        head, self.buf = self.buf.split(mark, 1)
        return head

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += self._recv()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode())

    def frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read(2)
        fin, op, n = bool(b0 & 0x80), b0 & 0x0F, b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        if n > MAX_MESSAGE:
            raise WebSocketError(f"frame too large ({n} bytes)")
        mask = self._read(4) if b1 & 0x80 else b""
        data = self._read(n)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return fin, op, data

    def _ready(self, wait_s: float) -> bool:
        """Data to read within wait_s (TLS bytes already decrypted count)."""
        if self.buf or (hasattr(self.sock, "pending") and self.sock.pending()):
            return True
        return bool(select.select([self.sock], [], [], wait_s)[0])

    def messages(self, idle_s: float | None = None):
        """Each whole text or binary message's bytes, until the server closes. idle_s: when no frame starts within that
        long, None is yielded (the caller's chance to give up quietly; the connection is untouched)."""
        parts, size = [], 0
        while not self.closed:
            if idle_s is not None and not parts and not self._ready(idle_s):
                yield None
                continue
            fin, op, data = self.frame()
            if op == 0x9:                       # ping: pong with its data
                self._send(0xA, data)
                continue
            if op == 0xA:
                continue
            if op == 0x8:                       # close: answer it and stop
                self.closed = True
                try:
                    self._send(0x8, data[:2])
                except OSError:
                    pass
                code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                why = data[2:].decode("utf-8", "replace")[:200]
                if code not in (1000, 1001, 1005):
                    raise WebSocketError(f"closed by the server ({code}{': ' + why if why else ''})")
                return
            if op in (0x1, 0x2, 0x0):
                parts.append(data)
                size += len(data)
                if size > MAX_MESSAGE:
                    raise WebSocketError("message too large")
                if fin:
                    yield b"".join(parts)
                    parts, size = [], 0
                continue
            raise WebSocketError(f"unknown opcode {op}")

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self._send(0x8, struct.pack(">H", 1000))
            except OSError:
                pass
        try:                                    # (wakes a read blocked in another thread)
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
