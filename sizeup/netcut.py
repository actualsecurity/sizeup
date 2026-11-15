"""Cutting a streaming read short from another thread (#546).

A thread blocked reading a long-lived HTTP stream (ntfy's JSON stream, sizeup/prealert.py) can't be stopped with
resp.close(): http.client's response reads through a BufferedReader, whose close() waits for the lock the blocked
read holds, i.e. until the next bytes arrive (ntfy: a keepalive every ~45 s). And a socket's close() doesn't wake a
read blocked on it either. shutdown() does: the read returns at once (end of stream or an error), and the reading
thread, seeing its stop flag, ends and closes the response itself.

    sock = socket_of(resp)       # the reader, once the response is open
    cut(sock)                    # any other thread: the reader wakes now

Nothing here sends anything (a shutdown only ends the connection), so a read-only module may use it.
"""

from __future__ import annotations

import socket


def socket_of(resp):
    """The socket under a urlopen() response (http.client.HTTPResponse: fp is a BufferedReader over a SocketIO); None
    when it isn't one (a test's fake, a response already closed). Under TLS this is the SSLSocket."""
    return getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)


def cut(sock) -> None:
    """Shut a connection down under whoever is reading it. The plain socket's shutdown, under TLS too: SSLSocket's own
    shutdown() drops the TLS object the blocked read is using (sizeup/transcribe.py WebSocket.abort does the same).
    None, or a socket already closed: nothing to do."""
    if sock is None:
        return
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, TypeError, ValueError):
        pass
