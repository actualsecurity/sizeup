"""Amazon Transcribe streaming for the important channels (user, 2026-10-08: "lets try it [AWS
Transcribe] ... test aws transcribe on the important channels like South 2 paging, Fire/EMS 15 and our
fireground for now"). Stdlib only (+numpy): a minimal RFC 6455 WebSocket client, AWS event-stream
framing and SigV4 query signing (sizeup/aws.py).

Billing ($0.024 a minute, at least 15 s per stream) is why it works the way it does: one long-lived
stream per channel, transmissions fed back to back, never one stream per transmission (a 3.7 s
transmission alone would be billed 15 s).

    AwsTranscriber.submit(channel, item, pcm)    pcm: int16, 8 kHz mono (the clips the hub keeps)
        one ChannelWorker thread per channel (South 2, FG1, tg:307 ...), one stream each:
        open on the first transmission (wss://transcribestreaming.<region>.amazonaws.com:8443/
        stream-transcription-websocket?language-code=en-US&media-encoding=pcm&sample-rate=8000
        [&vocabulary-name=sizeup-pcfd], presigned), the audio sent in CHUNK_S pieces at `pace` x real
        time, GAP_S of silence after each transmission so the results segment cleanly. Each
        transmission's place in the stream ([a, b) seconds of audio sent) is kept; the words of final
        results (their StartTime) are given to the transmission whose audio they fall in, so one result
        that runs over two transmissions is split between them.
        A transmission is done when no open (partial) result starts inside it and either a final result
        ended past it or `quiet_s` has gone by since its last audio was sent. A result still open after
        `settle_s` more gets up to `flush_s` of silence (billed) to finish, then the stream is ended
        (which makes Transcribe finalize everything).
        Idle: every stream is billed at least 15 s, so a burst of traffic should share one; the stream
        is ended `idle_s` (default 120) after the last transmission and opened again on demand.
        Transcribe ends a stream that gets no audio for 15 s, so while it waits it gets KEEPALIVE_AUDIO_S
        (0.1 s) of silence every KEEPALIVE_S (10 s): billed as audio, about 1 % of the idle time
        (AWS bills the audio streamed, not the time connected; set idle_s to ~10 if that ever changes).
        4 h: a stream near Transcribe's 4-hour limit is ended between transmissions and a new one opened.
        Errors (BadRequest, LimitExceeded, ServiceUnavailable, InternalFailure, a dropped connection):
        the transmissions not done yet are tried once more in a new stream after a backoff; then
        on_fail(item, why). No permission, bad credentials or the account itself refused (billing,
        no subscription: classify()) fail at once. Nothing is ever dropped silently.
    on_done(item, text, confidence)    text "" = nothing said (no_speech)
    on_fail(item, why)                 the caller falls back (the station's whisper) or says it failed
    on_late(item, text, confidence)    AWS's answer for a job that had timed out (its fallback ran already)

Never stuck (GitHub #195: Fire/EMS 15 "Transcribing" for 50 minutes): every job has an overall deadline
from when it was submitted, deadline_for(clip) = max(DEADLINE_MIN_S, DEADLINE_FACTOR x the clip's length);
the watchdog thread fails a job past it as "AWS timed out" (on_fail: the fallback) and a later answer goes
to on_late, never on_done. The first outcome of a job wins (done | failed | timeout). Every socket read and
write on a stream gives up after STREAM_IO_S (no send blocked forever on a dead connection). A channel
thread that makes no progress for STALL_S has its stream cut, and one still stuck KICK_GRACE_S later, or one
that died, is replaced by a new thread that takes its queue. An exception in a channel thread is logged and
the job it was on is retried, never lost.

Circuit breaker: AWS failing (stream errors, refusals, timeouts) with no success for BREAKER_AFTER_S and at
least BREAKER_MIN_FAILS times opens it: available() is False with why ("Amazon Transcribe failing since
12:05 (timed out ...)"), so new clips go straight to whisper (the hub tells the station: its channel
push), and every BREAKER_RETRY_S AWS is tried again (half-open: the next clips go to it); a success closes
it, a failure opens it for another BREAKER_RETRY_S. health(): the last success, the recent failures with
AWS's reason, the breaker (station health, Settings > Data sources).

Budget (Usage): billed seconds per calendar month (max(15 s, audio sent) per stream) in the CAD
database; when the month's cost reaches the limit no new audio is sent: available() says why and the
callers use the local model until the next month.

Custom vocabulary (Vocabulary): a phrase list pushed as `sizeup-pcfd` (CreateVocabulary /
UpdateVocabulary, polled with GetVocabulary until READY) and used by new streams once READY. AWS rules
(list form): letters, apostrophes and periods only, the words of a phrase joined with hyphens
("Port-Chester"), at most 256 characters; no digits, so numbers are spelled out the way they are said
on the air ("Engine 60" -> "Engine-sixty", "23 98" -> "twenty-three-ninety-eight"). Transcribe then
writes those words, so respell() turns "Engine sixty" back into "Engine 60" in the text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import threading
import time
import urllib.parse
import zlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np

from sizeup import aws

RATE = 8000
MODEL = "aws-transcribe"
SERVICE = "transcribe"
PRICE_PER_MIN = 0.024
MIN_BILLED_S = 15.0
MAX_STREAM_S = 4 * 3600 - 120     # end a stream between transmissions before Transcribe's 4 h limit
KEEPALIVE_S = 10.0               # Transcribe ends a stream that gets no audio for 15 s: this often ...
KEEPALIVE_AUDIO_S = 0.1          # ... this much silence while it waits for the next transmission
VOCABULARY = "sizeup-pcfd"
RETRIES = 1                      # a transmission caught in a failed stream is tried this many more times
DEADLINE_MIN_S = 60.0            # a job's overall deadline from when it was submitted: at least this ...
DEADLINE_FACTOR = 3.0            # ... or this many times its clip's length; then "AWS timed out" and the fallback
WATCHDOG_S = 2.0                 # how often the deadlines and the channel threads are checked
STREAM_IO_S = 20.0               # one socket send or read on a stream blocked this long fails (the stream breaks)
STALL_S = 30.0                   # a channel thread without progress this long is stuck: its stream is cut ...
KICK_GRACE_S = 30.0              # ... and if it is still stuck this long after, a new thread takes its queue
BREAKER_AFTER_S = 120.0          # AWS failing this long with no success in between ...
BREAKER_MIN_FAILS = 3            # ... and at least this many times: the breaker opens (new clips go to whisper)
BREAKER_RETRY_S = 300.0          # open: AWS is tried again (the next clips) this long after it last failed
RECENT_MAX = 20                  # failures kept for health()
BUDGET_TZ = ZoneInfo("America/New_York")   # the month the budget counts (the console's time zone)
RESERVE_FRACTION = 0.2           # past the monthly limit, the reserve channels keep AWS until the limit x 1.2 ...
RESERVE_CHANNELS = ("South 2", "tg:307")   # ... the pages (South 2) and Fire/EMS 15; everything else goes to whisper

# AWS's error types by what they mean for us (the streaming handshake's HTTP answer, the stream's exception
# events, the JSON APIs). An account that can't pay, isn't signed up for Transcribe or is suspended: the AWS
# common errors OptInRequired ("The AWS access key ID needs a subscription for the service", HTTP 403) and
# SubscriptionRequiredException, S3-style AccountProblem, EC2-style Blocked; a closed account's keys stop
# working (UnrecognizedClientException / InvalidClientTokenId). Any of them, or a refusal whose message talks
# about billing / payment / subscription / suspension, is "billing".
BILLING = frozenset(("OptInRequired", "SubscriptionRequiredException", "AccountProblem", "Blocked", "AccountBlocked",
                     "AccountSuspended", "AccountSuspendedException", "PaymentRequired", "HTTP 402"))
CREDENTIALS = frozenset(("NoCredentials", "UnrecognizedClientException", "InvalidClientTokenId", "InvalidSignatureException",
                         "SignatureDoesNotMatch", "ExpiredTokenException", "ExpiredToken", "IncompleteSignature",
                         "MissingAuthenticationToken", "MissingAuthenticationTokenException", "RequestExpired"))
PERMISSION = frozenset(("AccessDenied", "AccessDeniedException", "NotAuthorized", "HTTP 401", "HTTP 403"))
THROTTLING = frozenset(("LimitExceededException", "ThrottlingException", "Throttling", "TooManyRequestsException",
                        "ServiceQuotaExceededException", "HTTP 429"))
SERVICE_ERRORS = frozenset(("ServiceUnavailableException", "InternalFailureException", "InternalFailure", "ServiceUnavailable"))
HARD = frozenset(("billing", "credentials", "permission"))      # retrying won't help: fail at once
BILLING_WORDS = re.compile(r"subscri|billing|payment|suspend|opt.?in\b|not signed up|account (?:is |has been )?"
                           r"(?:blocked|closed|suspended|disabled|not (?:active|verified))", re.I)
REASON_WORDS = {"billing": "AWS refused the account (billing / subscription)", "credentials": "AWS credentials",
                "permission": "no permission (AccessDenied)", "throttling": "throttled by AWS", "timeout": "timed out",
                "service": "AWS service error", "network": "connection to AWS", "internal": "internal error",
                "other": "AWS error"}


def classify(kind: str, msg: str = "") -> str:
    """What an AWS failure means: billing | credentials | permission | throttling | timeout | service |
    network | internal | other (kind: AWS's error type or ours: "Timeout", "Connection", "Internal")."""
    kind = kind or ""
    if kind == "Timeout":
        return "timeout"
    if kind in BILLING:
        return "billing"
    if kind in THROTTLING:
        return "throttling"
    if kind in CREDENTIALS:
        return "credentials"
    if kind == "Internal":
        return "internal"
    network = kind in ("Connection", "Closed", "Network")
    if not network and kind not in SERVICE_ERRORS and BILLING_WORDS.search(msg or ""):
        return "billing"
    if kind in PERMISSION:
        return "permission"
    if kind in SERVICE_ERRORS or kind.startswith("HTTP 5"):
        return "service"
    return "network" if network else "other"


def deadline_for(clip_s: float, min_s: float = DEADLINE_MIN_S, factor: float = DEADLINE_FACTOR) -> float:
    """How long a job with clip_s seconds of audio may take, from submit to answer."""
    return max(float(min_s), float(factor) * max(0.0, float(clip_s)))


def _hhmm(t: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(t)) if t else "?"


# -- AWS event stream (application/vnd.amazon.eventstream) ---------------------------------------------------

class EventStreamError(ValueError):
    pass


def _headers_bytes(headers: dict) -> bytes:
    out = bytearray()
    for k, v in headers.items():
        kb = k.encode()
        if isinstance(v, (bytes, bytearray)):
            out += bytes([len(kb)]) + kb + b"\x06" + struct.pack(">H", len(v)) + bytes(v)
        else:
            vb = str(v).encode()
            out += bytes([len(kb)]) + kb + b"\x07" + struct.pack(">H", len(vb)) + vb
    return bytes(out)


def encode_message(headers: dict, payload: bytes) -> bytes:
    """prelude (total length, headers length, CRC32 of those 8 bytes), headers, payload, CRC32 of it all."""
    h = _headers_bytes(headers)
    prelude = struct.pack(">II", 16 + len(h) + len(payload), len(h))
    msg = prelude + struct.pack(">I", zlib.crc32(prelude)) + h + payload
    return msg + struct.pack(">I", zlib.crc32(msg))


def _decode_headers(b: bytes) -> dict:
    out, i = {}, 0
    while i < len(b):
        n = b[i]
        name = b[i + 1:i + 1 + n].decode()
        i += 1 + n
        t = b[i]
        i += 1
        if t in (0, 1):
            v: object = t == 0
        elif t == 2:
            v, i = struct.unpack(">b", b[i:i + 1])[0], i + 1
        elif t == 3:
            v, i = struct.unpack(">h", b[i:i + 2])[0], i + 2
        elif t == 4:
            v, i = struct.unpack(">i", b[i:i + 4])[0], i + 4
        elif t in (5, 8):
            v, i = struct.unpack(">q", b[i:i + 8])[0], i + 8
        elif t in (6, 7):
            ln = struct.unpack(">H", b[i:i + 2])[0]
            raw = b[i + 2:i + 2 + ln]
            v, i = (raw.decode() if t == 7 else raw), i + 2 + ln
        elif t == 9:
            v, i = b[i:i + 16].hex(), i + 16
        else:
            raise EventStreamError(f"header {name!r}: unknown type {t}")
        out[name] = v
    return out


def decode_message(buf: bytes) -> tuple[dict, bytes, int]:
    """(headers, payload, bytes used) of the message at the start of buf. EventStreamError on a bad CRC
    or a short buffer."""
    if len(buf) < 16:
        raise EventStreamError("short message")
    total, hlen = struct.unpack(">II", buf[:8])
    if struct.unpack(">I", buf[8:12])[0] != zlib.crc32(buf[:8]):
        raise EventStreamError("prelude CRC mismatch")
    if total < 16 + hlen or len(buf) < total:
        raise EventStreamError("short message")
    if struct.unpack(">I", buf[total - 4:total])[0] != zlib.crc32(buf[:total - 4]):
        raise EventStreamError("message CRC mismatch")
    return _decode_headers(buf[12:12 + hlen]), bytes(buf[12 + hlen:total - 4]), total


def audio_event(pcm: bytes) -> bytes:
    """One AudioEvent; an empty one ends the stream (Transcribe then sends its last results)."""
    return encode_message({":content-type": "application/octet-stream", ":event-type": "AudioEvent",
                           ":message-type": "event"}, pcm)


# -- WebSocket (RFC 6455, client side) --------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WsError(OSError):
    pass


class WsHandshakeError(WsError):
    """The server answered the upgrade with something else. code: AWS's error type when it said one
    (x-amzn-ErrorType, the JSON body's __type / code, an XML <Code>), else "HTTP <status>"; detail: AWS's
    own words ("SubscriptionRequiredException: The AWS Access Key Id needs a subscription ...")."""

    def __init__(self, status: int, body: str, code: str = "", detail: str = ""):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body
        self.code, self.detail = code or f"HTTP {status}", detail or f"HTTP {status}: {body[:300]}"


def _handshake_error(status: int, hdrs: dict, body: bytes) -> WsHandshakeError:
    err = aws.error_from(status, {"x-amzn-ErrorType": hdrs.get("x-amzn-errortype", "")}, body)
    code, detail = err.code, str(err)
    if code.startswith("HTTP "):
        m = re.search(rb"<Code>\s*([A-Za-z0-9.]+)\s*</Code>", body or b"")
        if m:
            code = m.group(1).decode()
            msg = re.search(rb"<Message>(.{0,300}?)</Message>", body or b"", re.S)
            detail = code + (f": {msg.group(1).decode(errors='replace')}" if msg else "")
    return WsHandshakeError(status, (body or b"").decode(errors="replace"), code, detail)


def _mask(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    a = np.frombuffer(data, np.uint8)
    k = np.frombuffer((key * (len(data) // 4 + 1))[:len(data)], np.uint8)
    return (a ^ k).tobytes()


class WebSocket:
    """A client connection: binary frames out (masked), frames in; ping answered with pong. Thread
    safe for one reader and any number of writers."""

    def __init__(self, sock: socket.socket, leftover: bytes = b""):
        self.sock, self._buf = sock, bytearray(leftover)
        self._wlock = threading.Lock()
        self.closed = False

    @classmethod
    def connect(cls, url: str, timeout: float = 10.0, headers: dict | None = None) -> WebSocket:
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise WsError(f"not a WebSocket URL: {u.scheme}://")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        try:
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "wss" else raw
            key = base64.b64encode(os.urandom(16)).decode()
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            lines = [f"GET {path} HTTP/1.1", f"Host: {u.netloc}", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
            lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            data = bytearray()
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WsError("connection closed during the WebSocket handshake")
                data += chunk
                if len(data) > 65536:
                    raise WsError("WebSocket handshake answer too long")
            head, _, rest = bytes(data).partition(b"\r\n\r\n")
            status_line, *hdr_lines = head.decode("latin-1").split("\r\n")
            parts = status_line.split(" ", 2)
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            hdrs = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in hdr_lines)}
            if status != 101:
                body = bytearray(rest)
                want = int(hdrs.get("content-length") or 0)
                try:
                    while len(body) < min(want, 65536):
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        body += chunk
                except OSError:
                    pass
                raise _handshake_error(status, hdrs, bytes(body))
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            if hdrs.get("sec-websocket-accept") != accept:
                raise WsError("bad Sec-WebSocket-Accept in the handshake")
            return cls(sock, rest)
        except BaseException:
            raw.close()
            raise

    def send_frame(self, opcode: int, payload: bytes = b"", lock_s: float | None = None) -> None:
        """lock_s: wait at most this long for another sender (close(): never behind a send blocked on a dead
        connection, #195); WsError when it can't."""
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        key = os.urandom(4)
        if not self._wlock.acquire(timeout=-1 if lock_s is None else lock_s):
            raise WsError("WebSocket busy: another send is stuck")
        try:
            if self.closed:
                raise WsError("WebSocket closed")
            self.sock.sendall(head + key + _mask(payload, key))
        finally:
            self._wlock.release()

    def send_binary(self, data: bytes) -> None:
        self.send_frame(OP_BINARY, data)

    def _frame(self) -> tuple[bool, int, bytes] | None:
        b = self._buf
        if len(b) < 2:
            return None
        fin, op, masked, ln, i = bool(b[0] & 0x80), b[0] & 0x0F, bool(b[1] & 0x80), b[1] & 0x7F, 2
        if ln == 126:
            if len(b) < 4:
                return None
            ln, i = struct.unpack(">H", bytes(b[2:4]))[0], 4
        elif ln == 127:
            if len(b) < 10:
                return None
            ln, i = struct.unpack(">Q", bytes(b[2:10]))[0], 10
        key = b""
        if masked:
            if len(b) < i + 4:
                return None
            key, i = bytes(b[i:i + 4]), i + 4
        if len(b) < i + ln:
            return None
        payload = bytes(b[i:i + ln])
        del b[:i + ln]
        return fin, op, (_mask(payload, key) if masked else payload)

    def recv(self) -> tuple[int, bytes]:
        """The next data message (OP_BINARY / OP_TEXT, fragments joined) or (OP_CLOSE, code + reason).
        socket.timeout (TimeoutError) when nothing came in time: nothing read so far is lost."""
        parts: list[bytes] = []
        first_op = None
        while True:
            fr = self._frame()
            if fr is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WsError("connection closed")
                self._buf += chunk
                continue
            fin, op, payload = fr
            if op == OP_PING:
                try:
                    self.send_frame(OP_PONG, payload)
                except OSError:
                    pass
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                try:
                    self.send_frame(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                return OP_CLOSE, payload
            if op != OP_CONT:
                first_op = op
            parts.append(payload)
            if fin:
                return (first_op if first_op is not None else OP_BINARY), b"".join(parts)

    def abort(self) -> None:
        """Cut the connection under whoever is blocked on it (a send or a read in another thread fails at
        once); close() still frees it. The plain socket's shutdown, under TLS too: no TLS state is touched."""
        try:
            socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
        except (OSError, TypeError, ValueError):
            pass

    def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        try:
            self.send_frame(OP_CLOSE, struct.pack(">H", code), lock_s=1.0)
        except OSError:
            pass
        self.closed = True
        self.abort()                          # (a reader blocked on it wakes: close() alone doesn't wake it)
        try:
            self.sock.close()
        except OSError:
            pass


# -- one stream -------------------------------------------------------------------------------------------

@dataclass
class Word:
    start: float
    end: float
    content: str
    kind: str = "pronunciation"          # | "punctuation"
    confidence: float | None = None


def words_text(words: list[Word]) -> str:
    out = ""
    for w in words:
        if w.kind == "punctuation":
            out += w.content
        else:
            out += (" " if out else "") + w.content
    return out.strip()


def _words(result: dict) -> list[Word]:
    alt = (result.get("Alternatives") or [{}])[0] or {}
    items = alt.get("Items") or []
    if not items:
        text = str(alt.get("Transcript") or "").strip()
        return [Word(float(result.get("StartTime") or 0), float(result.get("EndTime") or 0), text)] if text else []
    out = []
    for it in items:
        c = it.get("Confidence")
        out.append(Word(float(it.get("StartTime") or 0), float(it.get("EndTime") or 0), str(it.get("Content") or ""),
                        str(it.get("Type") or "pronunciation"), float(c) if isinstance(c, (int, float)) else None))
    return out


class Session:
    """One Transcribe stream: send audio, collect results (a reader thread)."""

    def __init__(self, ws: WebSocket, log: Callable[[str], None] = print, rate: int = RATE):
        self.ws, self.log, self.rate = ws, log, rate
        self.cond = threading.Condition()
        self.words: list[Word] = []                    # every final word so far
        self.partial: tuple[str, float, float, list[Word]] | None = None   # an open result: (id, start, end, words)
        self.final_end = 0.0
        self.error: tuple[str, str] | None = None      # (AWS exception type, message)
        self.closed = False
        self.sent_s = 0.0
        self.opened = self.last_sent = time.time()
        self.ended = False
        threading.Thread(target=self._read, name="aws-stt-read", daemon=True).start()

    def send(self, pcm: bytes) -> None:
        self.ws.send_binary(audio_event(pcm))
        self.sent_s += len(pcm) / 2 / self.rate
        self.last_sent = time.time()

    def end(self) -> None:
        if not self.ended:
            self.ended = True
            self.ws.send_binary(audio_event(b""))

    def _read(self) -> None:
        try:
            # Never None: the socket's timeout is its sends' too (one socket), and a send on a dead connection
            # with no timeout blocks the channel's thread for good (#195). Quiet between results is normal.
            self.ws.sock.settimeout(STREAM_IO_S)
            while True:
                try:
                    op, data = self.ws.recv()
                except TimeoutError:
                    if self.ws.closed:
                        break
                    continue
                if op == OP_CLOSE:
                    code = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 1005
                    if code not in (1000, 1005) and not self.ended:
                        self._fail("Closed", f"closed by Transcribe ({code} {data[2:].decode(errors='replace')})".strip())
                    break
                if op != OP_BINARY:
                    continue
                headers, payload, _ = decode_message(data)
                kind = headers.get(":message-type")
                if kind == "event" and headers.get(":event-type") == "TranscriptEvent":
                    self._results(json.loads(payload or b"{}"))
                elif kind in ("exception", "error"):
                    try:
                        msg = str(json.loads(payload or b"{}").get("Message") or "")
                    except ValueError:
                        msg = payload.decode(errors="replace")[:300]
                    self._fail(str(headers.get(":exception-type") or headers.get(":error-code") or "Exception"), msg)
                    break
        except (OSError, EventStreamError, ValueError) as e:
            if not self.closed and not (self.ended and isinstance(e, WsError)):
                self._fail("Connection", f"{type(e).__name__}: {e}")
        finally:
            with self.cond:
                self.closed = True
                self.cond.notify_all()
            self.ws.close()

    def _fail(self, kind: str, msg: str) -> None:
        with self.cond:
            if self.error is None:
                self.error = (kind, msg)
            self.cond.notify_all()

    def _results(self, d: dict) -> None:
        with self.cond:
            for r in (d.get("Transcript") or {}).get("Results") or []:
                rid = str(r.get("ResultId") or "")
                if r.get("IsPartial"):
                    self.partial = (rid, float(r.get("StartTime") or 0), float(r.get("EndTime") or 0), _words(r))
                    continue
                self.words.extend(_words(r))
                self.final_end = max(self.final_end, float(r.get("EndTime") or 0))
                if self.partial is not None and (self.partial[0] == rid or self.partial[1] < float(r.get("EndTime") or 0)):
                    self.partial = None
            self.cond.notify_all()

    def close(self) -> None:
        self.ws.close()


# -- usage and the monthly budget -------------------------------------------------------------------------

class Usage:
    """Billed seconds per calendar month (local time), kept through get/put (the CAD's meta table)."""

    def __init__(self, get: Callable[[str], str | None], put: Callable[[str, str], None],
                 now: Callable[[], float] = time.time):
        self._get, self._put, self.now = get, put, now
        self._lock = threading.Lock()

    def month_key(self) -> str:
        """The calendar month in the console's time zone (America/New_York), not the container's (UTC): the
        budget starts again at local midnight on the 1st."""
        return datetime.fromtimestamp(self.now(), BUDGET_TZ).strftime("%Y-%m")

    def month(self) -> dict:
        key = self.month_key()
        try:
            d = json.loads(self._get(f"aws_usage:{key}") or "{}")
        except ValueError:
            d = {}
        return {"month": key, "billed_s": float(d.get("billed_s") or 0), "audio_s": float(d.get("audio_s") or 0),
                "streams": int(d.get("streams") or 0)}

    def add(self, billed_s: float, audio_s: float) -> None:
        with self._lock:
            m = self.month()
            m["billed_s"] += billed_s
            m["audio_s"] += audio_s
            m["streams"] += 1
            self._put(f"aws_usage:{m['month']}", json.dumps({k: m[k] for k in ("billed_s", "audio_s", "streams")}))


@dataclass
class Settings:
    """[stt] aws_*: where and how (sizeup/config.py)."""
    region: str = ""
    endpoint: str = ""             # the streaming endpoint (tests: ws://127.0.0.1:port); "" = AWS's
    api_endpoint: str = ""         # the JSON API (vocabularies); "" = AWS's
    language: str = "en-US"
    idle_s: float = 120.0          # end a stream this long after its last transmission
    pace: float = 2.0              # audio sent this many times faster than real time
    gap_s: float = 0.3             # silence after each transmission
    quiet_s: float = 1.5           # a transmission is done this long after its audio went, if nothing is open in it
    settle_s: float = 2.0          # ... a result still open in it after this gets silence to finish
    flush_s: float = 1.5           # ... at most this much; then the stream is ended
    chunk_s: float = 0.1
    price_per_min: float = PRICE_PER_MIN
    vocabulary: str = VOCABULARY
    deadline_min_s: float = DEADLINE_MIN_S     # the watchdog and the breaker (the module doc); tests make them short
    deadline_factor: float = DEADLINE_FACTOR
    watchdog_s: float = WATCHDOG_S
    stall_s: float = STALL_S
    kick_grace_s: float = KICK_GRACE_S
    breaker_after_s: float = BREAKER_AFTER_S
    breaker_min_fails: int = BREAKER_MIN_FAILS
    breaker_retry_s: float = BREAKER_RETRY_S
    reserve_fraction: float = RESERVE_FRACTION
    reserve_channels: tuple = RESERVE_CHANNELS


@dataclass(eq=False)
class Job:
    channel: str
    item: object
    pcm: np.ndarray
    attempts: int = 0
    a: float = 0.0                 # where its audio starts / ends in the stream (s)
    b: float = 0.0
    sent_at: float = 0.0
    flushed: float = 0.0
    done: bool = False
    submitted: float = 0.0
    deadline: float = float("inf")  # past this (the owner's clock) it fails as "AWS timed out"
    state: str = "open"            # open | done | failed | timeout: the first outcome wins

    @property
    def seconds(self) -> float:
        return len(self.pcm) / RATE


# -- the service ------------------------------------------------------------------------------------------

class AwsTranscriber:
    def __init__(self, s: Settings, usage: Usage, on_done: Callable[[object, str, float | None], None],
                 on_fail: Callable[[object, str], None], log: Callable[[str], None] = print,
                 limit_usd: Callable[[], float] = lambda: 25.0, connect: Callable[..., WebSocket] = WebSocket.connect,
                 creds: Callable[[], aws.Credentials] = aws.credentials, respell: Callable[[str], str] = lambda t: t,
                 on_late: Callable[[object, str, float | None], None] = lambda item, text, conf: None,
                 on_change: Callable[[], None] = lambda: None, clock: Callable[[], float] = time.time):
        self.s, self.usage, self.on_done, self.on_fail, self.log = s, usage, on_done, on_fail, log
        self.limit_usd, self.connect, self.creds, self.respell = limit_usd, connect, creds, respell
        self.on_late, self.on_change, self.clock = on_late, on_change, clock
        self.region = aws.region(s.region)
        self.vocabulary = ""                       # set once the custom vocabulary is READY
        self.pace = max(0.5, float(s.pace))
        self._workers: dict[str, ChannelWorker] = {}
        self._lock = threading.RLock()
        self.stopped = threading.Event()
        self.last_error, self.last_error_at = "", None
        self.done_n = self.failed_n = self.timeouts_n = self.late_n = self.replaced_n = 0
        self._open: set[Job] = set()               # submitted, no outcome yet: what the watchdog watches
        self._watchdog: threading.Thread | None = None
        # health and the circuit breaker (the module doc)
        self.last_ok: float | None = None
        self.failing_since: float | None = None   # the first failure since the last success
        self.streak = 0                            # failures since the last success
        self.recent: deque[dict] = deque(maxlen=RECENT_MAX)
        self.breaker: dict = {"state": "closed", "since": None, "retry_at": None}

    def deadline_for(self, clip_s: float) -> float:
        return deadline_for(clip_s, self.s.deadline_min_s, self.s.deadline_factor)

    # -- what the callers ask --------------------------------------------------------------------------

    def url(self) -> str:
        base = (self.s.endpoint or f"wss://transcribestreaming.{self.region}.amazonaws.com:8443").rstrip("/")
        q = {"language-code": self.s.language, "media-encoding": "pcm", "sample-rate": str(RATE)}
        if self.vocabulary:
            q["vocabulary-name"] = self.vocabulary
        return aws.presign(f"{base}/stream-transcription-websocket?{urllib.parse.urlencode(q)}", self.creds(),
                           self.region, SERVICE, expires=300)

    def spent(self) -> dict:
        """This month: billed seconds (closed streams + the open ones so far), minutes, dollars, the limit."""
        m = self.usage.month()
        live = sum(max(MIN_BILLED_S, w.session.sent_s) for w in list(self._workers.values()) if w.session is not None)
        billed = m["billed_s"] + live
        return {**m, "billed_s": round(billed, 1), "minutes": round(billed / 60, 1),
                "usd": round(billed / 60 * self.s.price_per_min, 2), "limit_usd": float(self.limit_usd()),
                "streams_open": sum(1 for w in list(self._workers.values()) if w.session is not None)}

    def budget_ok(self, extra_s: float = 0.0, channel: str | None = None) -> tuple[bool, str]:
        """Within this month's limit; a reserve channel (pages, Fire/EMS 15) up to the limit and its reserve."""
        sp = self.spent()
        limit = sp["limit_usd"]
        cost = (sp["billed_s"] + extra_s) / 60 * self.s.price_per_min
        if cost < limit:
            return True, ""
        cap = limit * (1 + max(0.0, self.s.reserve_fraction))
        if channel in self.s.reserve_channels and cost < cap:
            return True, ""
        if channel in self.s.reserve_channels and cap > limit:
            return False, (f"the monthly AWS limit and its reserve for pages and Fire/EMS 15 are used up "
                           f"(${sp['usd']:.2f} of ${cap:g} in {sp['month']})")
        return False, f"the monthly AWS limit is reached (${sp['usd']:.2f} of ${limit:g} in {sp['month']})"

    def reserve_left(self) -> list[str]:
        """The channels that may still use AWS past the monthly limit (none once the reserve is used too)."""
        return [c for c in self.s.reserve_channels if self.budget_ok(MIN_BILLED_S, c)[0]]

    def budget(self) -> dict:
        """This month's cost against the limit (station health: the 80 % and 100 % alerts)."""
        sp = self.spent()
        limit = sp["limit_usd"]
        pct = round(100.0 * sp["usd"] / limit, 1) if limit > 0 else 100.0
        left = self.reserve_left() if pct >= 100 else list(self.s.reserve_channels)
        cap = limit * (1 + max(0.0, self.s.reserve_fraction))
        return {"month": sp["month"], "usd": sp["usd"], "limit_usd": limit, "pct": pct, "cap_usd": round(cap, 2),
                "reserve": left, "reserve_text": (f"pages and Fire/EMS 15 keep AWS up to ${cap:g} (the reserve)" if left
                                                  else "the reserve for pages and Fire/EMS 15 is used up too")
                if pct >= 100 else ""}

    def breaker_ok(self) -> tuple[bool, str]:
        """(False, why) while the breaker is open. Past its retry time it half-opens: the next clips go to AWS."""
        now = self.clock()
        half = False
        with self._lock:
            b = self.breaker
            if b["state"] == "open" and now >= (b["retry_at"] or 0.0):
                b["state"], half = "half-open", True
            why = self._open_why() if b["state"] == "open" else ""
        if half:
            self.log("aws transcribe: trying AWS again (the breaker is half-open): the next clips go to it")
            self._changed()
        return not why, why

    def _open_why(self) -> str:
        last = self.recent[0] if self.recent else {}
        return (f"Amazon Transcribe failing since {_hhmm(self.failing_since)} "
                f"({REASON_WORDS.get(str(last.get('reason') or ''), 'AWS error')}: {str(last.get('why') or '')[:160]}): "
                f"whisper until AWS answers again (next try {_hhmm(self.breaker['retry_at'])})")

    def available(self, channel: str | None = None) -> tuple[bool, str]:
        """(True, "") when a transmission sent now would go to AWS (from that channel: the reserve); else why not."""
        ok, why = self.breaker_ok()
        if not ok:
            return False, why
        try:
            self.creds()
        except aws.AwsError as e:
            return False, str(e)
        return self.budget_ok(MIN_BILLED_S, channel)

    def retry_now(self) -> bool:
        """Settings > Data sources, Try AWS now: an open breaker half-opens (the next clips go to AWS)."""
        with self._lock:
            if self.breaker["state"] == "open":
                self.breaker["retry_at"] = self.clock()
        self.breaker_ok()
        return True

    def submit(self, channel: str, item, pcm: np.ndarray) -> None:
        pcm = np.asarray(pcm, dtype=np.int16)
        now = self.clock()
        job = Job(channel, item, pcm, submitted=now)
        job.deadline = now + self.deadline_for(job.seconds)
        if self.stopped.is_set():
            return self._fail(job, "speech-to-text stopped")
        with self._lock:
            self._open.add(job)
            w = self._workers.get(channel)
            if w is None or w.abandoned or not w.is_alive():
                w = self._new_worker(channel, w, "its thread had stopped")
            w.put(job)
        self._watch()

    def give_up(self, match: Callable[[object], bool], why: str) -> bool:
        """Fail every open job whose item matches as timed out (on_fail: its fallback). The hub's sweep, for an
        item still "Transcribing" past its deadline. True if there was one."""
        with self._lock:
            jobs = sorted((j for j in self._open if match(j.item)), key=lambda j: j.submitted)
        return any([self._timeout(j, why) for j in jobs])

    def status(self) -> dict:
        return {"region": self.region, "vocabulary": self.vocabulary, "pace": self.pace,
                "workers": {c: w.state() for c, w in list(self._workers.items())},
                "last_error": self.last_error, "last_error_at": self.last_error_at,
                "done": self.done_n, "failed": self.failed_n, "timeouts": self.timeouts_n, "late": self.late_n,
                "health": self.health()}

    def health(self) -> dict:
        """How AWS is doing: state ok | failing (failures since the last success) | down (the breaker is open:
        whisper takes every clip) | retrying (half-open), a line saying it, the last success, the recent
        failures with AWS's reason (classify()), the breaker, the jobs open now."""
        self.breaker_ok()
        now = self.clock()
        with self._lock:
            b = dict(self.breaker)
            recent = [dict(r) for r in self.recent]
            oldest = min((j.submitted for j in self._open), default=None)
            n_open = len(self._open)
            failing, streak, last_ok = self.failing_since, self.streak, self.last_ok
            why_open = self._open_why() if b["state"] == "open" else ""
        state = {"open": "down", "half-open": "retrying"}.get(b["state"]) or ("failing" if failing else "ok")
        last = recent[0] if recent else None
        reason = str(last["reason"]) if last and failing else ""
        words = REASON_WORDS.get(reason, "")
        if state == "down":
            text = why_open
        elif state == "retrying":
            text = f"failing since {_hhmm(failing)} ({words}): trying AWS again with the next clips"
        elif state == "failing":
            text = f"{streak} failure(s) since {_hhmm(failing)} ({words}: {str(last['why'])[:160]})" if last else "failing"
        else:
            text = f"last answer {_hhmm(last_ok)}" if last_ok else "no answer yet since the hub started"
        return {"on": True, "state": state, "text": text, "reason": reason, "reason_text": words,
                "last_ok": last_ok, "failing_since": failing, "failures": streak,
                "last_fail": last["at"] if last else None, "last_error": str(last["why"]) if last else "",
                "recent": recent[:10], "breaker": b, "done": self.done_n, "failed": self.failed_n,
                "timeouts": self.timeouts_n, "late": self.late_n, "threads_replaced": self.replaced_n,
                "open_jobs": n_open, "oldest_s": round(now - oldest, 1) if oldest is not None else None,
                "budget": self._budget_safe(),
                "limits": {"deadline_min_s": self.s.deadline_min_s, "deadline_factor": self.s.deadline_factor,
                           "breaker_after_s": self.s.breaker_after_s, "breaker_min_fails": self.s.breaker_min_fails,
                           "breaker_retry_s": self.s.breaker_retry_s}}

    def _budget_safe(self) -> dict:
        try:
            return self.budget()
        except Exception as e:  # (the cost line's SQLite) never in the way of the health
            return {"error": f"{type(e).__name__}: {e}"}

    def stop(self) -> None:
        self.stopped.set()
        for w in list(self._workers.values()):
            w.wake()
        for w in list(self._workers.values()):
            w.join(timeout=8)

    # -- the watchdog -------------------------------------------------------------------------------------

    def _new_worker(self, channel: str, old: ChannelWorker | None, why: str) -> ChannelWorker:
        """(under _lock) A thread for the channel; one replacing `old` (dead or stuck) takes its jobs."""
        w = ChannelWorker(self, channel)
        if old is not None:
            moved = old.take_over()
            w.q.extend(moved)
            self.replaced_n += 1
            self.log(f"aws transcribe {channel}: {why}: a new thread takes its {len(moved)} job(s)")
        self._workers[channel] = w
        w.start()
        return w

    def _watch(self) -> None:
        with self._lock:
            if self._watchdog is not None or self.stopped.is_set():
                return
            self._watchdog = threading.Thread(target=self._watch_loop, name="aws-stt-watchdog", daemon=True)
            self._watchdog.start()

    def _watch_loop(self) -> None:
        while not self.stopped.wait(max(0.01, self.s.watchdog_s)):
            try:
                self.check()
            except Exception as e:  # the watchdog must never die
                try:
                    self.log(f"aws transcribe: watchdog: {type(e).__name__}: {e}")
                except Exception:
                    pass

    def check(self) -> int:
        """One look (the watchdog thread, every watchdog_s): every job past its deadline fails as "AWS timed
        out" (on_fail: its fallback; a later answer goes to on_late); a channel thread with no progress for
        stall_s has its stream cut, and one still stuck kick_grace_s later, or one that died, is replaced by a
        new thread with its jobs. An open breaker past its retry time half-opens. How many timed out."""
        now = self.clock()
        with self._lock:
            due = sorted((j for j in self._open if now >= j.deadline), key=lambda j: j.submitted)
        for j in due:
            self._timeout(j, f"AWS timed out: no answer in {j.deadline - j.submitted:.0f} s")
        self.breaker_ok()
        t = time.time()
        for ch, w in list(self._workers.items()):
            if self.stopped.is_set():
                break
            if not w.is_alive():
                with self._lock:
                    if self._workers.get(ch) is w:
                        self._new_worker(ch, w, "its thread died")
                continue
            if w.idle or t - w.beat < self.s.stall_s:
                continue
            if w.kicked <= w.beat:
                w.kick(f"no progress for {t - w.beat:.0f} s")
            elif t - w.kicked >= self.s.kick_grace_s:
                with self._lock:
                    if self._workers.get(ch) is w:
                        self._new_worker(ch, w, f"still stuck {t - w.kicked:.0f} s after its stream was cut")
        return len(due)

    # -- outcomes (the first one wins) ----------------------------------------------------------------------

    def _resolve(self, job: Job, state: str) -> bool:
        with self._lock:
            if job.state != "open":
                return False
            job.state = state
            self._open.discard(job)
            return True

    def _done(self, job: Job, text: str, conf: float | None) -> None:
        try:
            text = self.respell(text)
        except Exception as e:
            self.log(f"aws transcribe: respell: {type(e).__name__}: {e}")
        if self._resolve(job, "done"):
            self.done_n += 1
            self._record_ok()
            try:
                self.on_done(job.item, text, conf)
            except Exception as e:  # the caller's trouble must not stop the stream
                self.log(f"aws transcribe: could not apply a result: {type(e).__name__}: {e}")
            return
        if job.state == "timeout":                   # its fallback ran: the caller decides what a late text is
            self.late_n += 1
            self.log(f"aws transcribe {job.channel}: an answer {max(0.0, self.clock() - job.deadline):.0f} s "
                     "after its deadline (the fallback took it)")
            try:
                self.on_late(job.item, text, conf)
            except Exception as e:
                self.log(f"aws transcribe: could not apply a late result: {type(e).__name__}: {e}")

    def _fail(self, job: Job, why: str) -> None:
        if not self._resolve(job, "failed"):
            return
        self.failed_n += 1
        self.last_error, self.last_error_at = why, time.time()
        try:
            self.on_fail(job.item, why)
        except Exception as e:
            self.log(f"aws transcribe: fallback failed: {type(e).__name__}: {e}")

    def _timeout(self, job: Job, why: str) -> bool:
        if not self._resolve(job, "timeout"):
            return False
        self.failed_n += 1
        self.timeouts_n += 1
        self.last_error, self.last_error_at = why, time.time()
        self.log(f"aws transcribe {job.channel}: {why}: the fallback takes it")
        self._record_fail("timeout", why, job.channel)
        try:
            self.on_fail(job.item, why)
        except Exception as e:
            self.log(f"aws transcribe: fallback failed: {type(e).__name__}: {e}")
        return True

    def _record_fail(self, reason: str, why: str, channel: str) -> None:
        """An AWS failure (a stream that broke with work in it, a refusal, a timeout): health and the breaker."""
        now = self.clock()
        opened = ""
        with self._lock:
            self.recent.appendleft({"at": now, "reason": reason, "why": why[:300], "channel": channel})
            if self.failing_since is None:
                self.failing_since, self.streak = now, 0
            self.streak += 1
            b = self.breaker
            if b["state"] == "half-open" or (b["state"] == "closed" and self.streak >= self.s.breaker_min_fails
                                              and now - self.failing_since >= self.s.breaker_after_s):
                if b["state"] == "closed":
                    b["since"] = now
                b["state"], b["retry_at"] = "open", now + self.s.breaker_retry_s
                opened = self._open_why()
        if opened:
            self.log(f"aws transcribe: breaker open, new clips go to whisper: {opened}")
            self._changed()

    def _record_ok(self) -> None:
        with self._lock:
            self.last_ok = self.clock()
            was = self.breaker["state"]
            self.failing_since, self.streak = None, 0
            self.breaker = {"state": "closed", "since": None, "retry_at": None}
        if was != "closed":
            self.log("aws transcribe: AWS answering again: the breaker is closed, the AWS channels use it again")
            self._changed()

    def _changed(self) -> None:
        try:
            self.on_change()
        except Exception as e:
            self.log(f"aws transcribe: on_change: {type(e).__name__}: {e}")

    def _bill(self, sent_s: float) -> None:
        if sent_s <= 0:
            return
        try:
            self.usage.add(max(MIN_BILLED_S, sent_s), sent_s)
        except Exception as e:  # the cost line's trouble (SQLite) must never kill a channel's thread (#195)
            self.log(f"aws transcribe: could not count {sent_s:.0f} s of audio: {type(e).__name__}: {e}")


class ChannelWorker(threading.Thread):
    """One channel's stream: feeds its transmissions, maps results back, opens and ends the stream."""

    def __init__(self, owner: AwsTranscriber, channel: str):
        super().__init__(name=f"aws-stt-{channel}", daemon=True)
        self.o, self.channel = owner, channel
        self.q: deque[Job] = deque()
        self.cv = threading.Condition()
        self.session: Session | None = None
        self.fed: list[Job] = []          # every job fed into this stream (for mapping words)
        self.pending: list[Job] = []      # fed, not done yet
        self.last_fed = 0.0
        self.errors = 0
        self.opened_n = 0
        self.current: Job | None = None   # the job taken off the queue now
        self.beat = time.time()           # its last sign of progress (the watchdog's stall check)
        self.kicked = 0.0                 # when the watchdog last cut its stream
        self.hold_until = 0.0             # a failed stream's backoff: nothing is fed before this
        self.abandoned = False            # replaced (stuck or dead): it delivers nothing more
        self.idle = False                 # waiting for work (never a stall)

    def put(self, job: Job) -> None:
        with self.cv:
            self.q.append(job)
            self.cv.notify()

    def wake(self) -> None:
        with self.cv:
            self.cv.notify()

    def state(self) -> dict:
        s = self.session
        return {"open": s is not None, "sent_s": round(s.sent_s, 1) if s else 0.0, "queued": len(self.q),
                "pending": len(self.pending), "opened": self.opened_n, "alive": self.is_alive(),
                "idle_s": round(time.time() - self.beat, 1)}

    def kick(self, why: str) -> None:
        """The watchdog: it is stuck (a send or read on a dead connection): cut its stream under it."""
        self.kicked = time.time()
        s = self.session
        self.o.log(f"aws transcribe {self.channel}: {why}: cutting its stream")
        if s is not None:
            s.ws.abort()

    def take_over(self) -> list[Job]:
        """It is replaced: its queue and the jobs in its stream with no answer yet, oldest first, for the new
        thread (fed again from the start). It touches none of them again."""
        with self.cv:
            self.abandoned = True
            jobs = ([self.current] if self.current is not None else []) + list(self.pending) + list(self.q)
            self.q.clear()
        out, seen = [], set()
        for j in jobs:
            if j.state == "open" and id(j) not in seen:
                seen.add(id(j))
                out.append(j)
        return sorted(out, key=lambda j: j.submitted)

    def run(self) -> None:
        while not self.o.stopped.is_set() and not self.abandoned:
            self.beat = time.time()
            job = None
            with self.cv:
                now = time.time()
                if not self.q or now < self.hold_until:
                    self.idle = True                  # (waiting for work is no stall)
                    self.cv.wait(0.05 if self.session is not None else
                                 1.0 if not self.q else min(1.0, max(0.01, self.hold_until - now)))
                    self.idle = False
                self.beat = time.time()
                if self.q and time.time() >= self.hold_until and not self.abandoned:
                    job = self.q.popleft()
            if job is not None and job.state != "open":
                job = None                        # its deadline passed while it waited: its fallback has it
            self.current = job                    # (kept if the thread dies on it: its replacement takes it)
            try:
                if job is not None:
                    self._feed(job)
                self._settle()
                self._idle()
            except Exception as e:  # never let one channel's thread die, never lose the job it was on
                self._crashed(e, job)
            self.current = None
        if self.abandoned:                         # replaced: the new thread has its jobs; only bill what went
            s, self.session = self.session, None
            if s is not None:
                try:
                    s.close()
                finally:
                    self.o._bill(s.sent_s)
            return
        try:
            if self.session is not None:
                self._end()
        except Exception as e:
            self.o.log(f"aws transcribe {self.channel}: ending its stream: {type(e).__name__}: {e}")
        for j in list(self.q):
            self.o._fail(j, "speech-to-text stopped")

    def _crashed(self, e: Exception, job: Job | None) -> None:
        why = f"{type(e).__name__}: {e}"
        try:
            self.o.log(f"aws transcribe {self.channel}: {why}")
            if job is not None and job.state == "open" and job not in self.pending:
                self.pending.append(job)          # off the queue, not in the stream yet: retried, not lost
            self._broken(("Internal", why))
        except Exception as e2:  # even the clean-up failed: start afresh; what it held fails over
            jobs = [j for j in self.pending if j.state == "open"]
            self.session, self.pending, self.fed = None, [], []
            for j in jobs:
                self.o._fail(j, f"AWS Transcribe: Internal: {why}"[:300])
            try:
                self.o.log(f"aws transcribe {self.channel}: clean-up failed too: {type(e2).__name__}: {e2}")
            except Exception:
                pass

    # -- the stream ------------------------------------------------------------------------------------

    def _open(self) -> None:
        url = self.o.url()
        ws = self.o.connect(url, timeout=10.0)
        self.session = Session(ws, self.o.log)
        self.fed, self.pending = [], []
        self.opened_n += 1

    def _feed(self, job: Job) -> None:
        o = self.o
        s = self.session
        if s is not None and (s.error or s.closed):
            self._broken(s.error or ("Closed", "the stream closed"))
            s = None
        if s is not None and (s.sent_s + job.seconds + o.s.gap_s > MAX_STREAM_S or time.time() - s.opened > MAX_STREAM_S):
            self._end()                                   # Transcribe's 4 h limit: a fresh stream
            s = None
        ok, why = o.breaker_ok()                          # AWS failing: whisper takes it now, not after a timeout
        if ok:
            ok, why = o.budget_ok(job.seconds + (MIN_BILLED_S if s is None else 0.0), self.channel)
        if not ok:
            return o._fail(job, why)
        if s is None:
            try:
                self._open()
            except (OSError, aws.AwsError) as e:
                return self._retry([job], self._kind(e), self._msg(e))
            s = self.session
        step = max(1, int(o.s.chunk_s * RATE))
        pcm = job.pcm.astype("<i2").tobytes()
        job.a, job.flushed = s.sent_s, 0.0
        try:
            for i in range(0, len(pcm), step * 2):
                if s.error or s.closed or self.abandoned:
                    break
                s.send(pcm[i:i + step * 2])
                self.beat = time.time()
                time.sleep(o.s.chunk_s / o.pace)
            if self.abandoned:
                return                                    # replaced while it was stuck: the job is the new thread's
            job.b = s.sent_s
            s.send(bytes(int(o.s.gap_s * RATE) * 2))
        except OSError as e:
            if self.abandoned:
                return
            self.fed.append(job)
            self.pending.append(job)
            return self._broken(self._why(s, e))
        job.sent_at = self.last_fed = time.time()
        self.fed.append(job)
        self.pending.append(job)
        if s.error or s.closed:
            self._broken(s.error or ("Closed", "the stream closed"))

    def _owner(self, t: float) -> Job | None:
        owner = None
        for j in self.fed:
            if j.a <= t + 1e-6:
                owner = j
            else:
                break
        return owner or (self.fed[0] if self.fed else None)

    def _finish(self, job: Job, words: list[Word]) -> None:
        mine = [w for w in words if self._owner(w.start) is job]
        conf = [w.confidence for w in mine if w.confidence is not None and w.kind != "punctuation"]
        job.done = True
        if self.abandoned:                     # replaced: the new thread answers for it
            return
        self.o._done(job, words_text(mine), round(float(np.mean(conf)), 2) if conf else None)

    def _settle(self) -> None:
        s, o = self.session, self.o
        if s is None:
            return
        if s.error or (s.closed and self.pending):
            return self._broken(s.error or ("Closed", "the stream closed"))
        while self.pending:
            now = time.time()
            j = self.pending[0]
            with s.cond:
                partial, final_end, words = s.partial, s.final_end, list(s.words)
            open_in = partial is not None and partial[1] < j.b - 1e-6
            if not open_in and (final_end >= j.b - 1e-6 or now - j.sent_at >= o.s.quiet_s):
                self.pending.pop(0)
                self._finish(j, words)
                continue
            if open_in and not self.q and now - j.sent_at >= o.s.quiet_s + o.s.settle_s:
                if j.flushed < o.s.flush_s:              # a little silence (billed) so the open result ends
                    try:
                        s.send(bytes(int(0.2 * RATE) * 2))
                        j.flushed += 0.2
                    except OSError as e:
                        return self._broken(self._why(s, e))
                else:
                    self._end()                           # end the stream: Transcribe finalizes everything
                    return
            break

    def _idle(self) -> None:
        s, o = self.session, self.o
        if s is None or self.pending or self.q:
            return
        now = time.time()
        if now - self.last_fed >= o.s.idle_s or now - s.opened >= MAX_STREAM_S:
            return self._end()
        if now - s.last_sent >= KEEPALIVE_S:               # keep it open: a little silence (billed)
            try:
                s.send(bytes(int(KEEPALIVE_AUDIO_S * RATE) * 2))
            except OSError as e:
                self._broken(self._why(s, e))

    def _end(self) -> None:
        """End the stream: everything not done yet gets what Transcribe finalizes, then it is billed."""
        s = self.session
        if s is None:
            return
        try:
            if not s.closed and not s.error:
                s.end()
                with s.cond:
                    s.cond.wait_for(lambda: s.closed or s.error is not None, timeout=8.0)
        except OSError:
            pass
        if s.error and self.pending:
            return self._broken(s.error)
        with s.cond:
            words = list(s.words)
            if s.partial is not None:                     # never finalized: its words as they last stood
                words += [w for w in s.partial[3] if w.start >= s.final_end - 1e-6]
        for j in self.pending:
            self._finish(j, words)
        self.pending = []
        self.session = None
        self.fed = []
        s.close()
        self.o._bill(s.sent_s)
        self.errors = 0

    @staticmethod
    def _why(s: Session, e: Exception) -> tuple[str, str]:
        """A send failed: Transcribe's own reason when it gave one (its exception closes the connection)."""
        with s.cond:
            s.cond.wait_for(lambda: s.closed or s.error is not None, timeout=1.0)
            return s.error or ("Connection", f"{type(e).__name__}: {e}")

    @staticmethod
    def _kind(e: Exception) -> str:
        if isinstance(e, WsHandshakeError):
            if not e.code.startswith("HTTP "):
                return e.code                             # AWS's own type (SubscriptionRequiredException ...)
            return "AccessDenied" if e.status == 403 else e.code
        if isinstance(e, aws.AwsError):
            return e.code or "AwsError"
        return "Connection"

    @staticmethod
    def _msg(e: Exception) -> str:
        return e.detail if isinstance(e, WsHandshakeError) else str(e)

    def _broken(self, err: tuple[str, str]) -> None:
        """The stream failed: bill what was sent, retry what wasn't done."""
        s = self.session
        jobs = [j for j in self.pending if not j.done]
        self.pending, self.fed, self.session = [], [], None
        if s is not None:
            s.close()
            self.o._bill(s.sent_s)
        kind, msg = err
        self.o.log(f"aws transcribe {self.channel}: {kind}: {msg}")
        if kind == "BadRequestException" and re.search(r"real.?time|too fast|rate", msg, re.I) and self.o.pace > 1.0:
            self.o.pace = 1.0
            self.o.log("aws transcribe: sending in real time from now on")
        self._retry(jobs, kind, msg)

    def _retry(self, jobs: list[Job], kind: str, msg: str) -> None:
        """Jobs caught in a failed stream (or one that wouldn't open): tried once more after a backoff (the
        thread keeps going meanwhile: nothing is fed before hold_until), then failed; at once when retrying
        can't help (no permission, credentials, the account refused: HARD)."""
        if self.abandoned:
            return
        self.errors += 1
        why = f"AWS Transcribe: {kind}: {msg}"[:300]
        reason = classify(kind, msg)
        self.o.last_error, self.o.last_error_at = why, time.time()
        jobs = [j for j in jobs if j.state == "open"]
        if jobs or reason in HARD:                        # (an idle stream that dropped is no failure of AWS's)
            self.o._record_fail(reason, why, self.channel)
        if reason in HARD:
            aws.forget_credentials()
            for j in jobs:
                self.o._fail(j, why)
            return
        again = [j for j in jobs if j.attempts < RETRIES]
        for j in jobs:
            if j.attempts >= RETRIES:
                self.o._fail(j, why)
        if not again:
            return
        delay = min(60.0, (5.0 if kind == "LimitExceededException" else 1.0) * 2 ** min(8, self.errors - 1))
        with self.cv:
            self.hold_until = time.time() + delay
            for j in reversed(again):
                j.attempts += 1
                j.a = j.b = j.sent_at = j.flushed = 0.0
                self.q.appendleft(j)


# -- custom vocabulary ------------------------------------------------------------------------------------

ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
        "seventeen eighteen nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
MAX_PHRASE = 256
MAX_PHRASES = 500


def _two(n: int) -> str:
    return ONES[n] if n < 20 else TENS[n // 10] + (f" {ONES[n % 10]}" if n % 10 else "")


def spoken_number(d: str) -> str:
    """Digits as said on the radio: 60 -> sixty, 31 -> thirty one, 401 -> four oh one, 2398 -> twenty
    three ninety eight, 1500 -> fifteen hundred; longer: digit by digit."""
    n = len(d)
    if n == 1:
        return ONES[int(d)]
    if n == 2:
        return f"oh {ONES[int(d[1])]}" if d[0] == "0" else _two(int(d))
    if n == 3:
        rest = "hundred" if d[1:] == "00" else f"oh {ONES[int(d[2])]}" if d[1] == "0" else _two(int(d[1:]))
        return f"{ONES[int(d[0])]} {rest}"
    if n == 4 and d[0] != "0":
        if d[2:] == "00":
            return f"{_two(int(d[:2]))} hundred"
        return f"{_two(int(d[:2]))} " + (f"oh {ONES[int(d[3])]}" if d[2] == "0" else _two(int(d[2:])))
    return " ".join(ONES[int(c)] for c in d)


def spoken(phrase: str) -> str:
    """A phrase in words: digits spelled out, '/' and '&' said."""
    p = phrase.replace("&", " and ").replace("/", " ").replace("_", " ")
    p = re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", p)
    p = re.sub(r"\d+", lambda m: f" {spoken_number(m.group())} ", p)
    return " ".join(p.split())


def to_aws(phrase: str) -> tuple[str | None, str]:
    """(the phrase as AWS takes it, "") or (None, why it can't go)."""
    words = re.sub(r"[^A-Za-z'. -]", " ", spoken(phrase)).replace("-", " ").split()
    words = [w.strip(".'") if not re.fullmatch(r"(?:[A-Za-z]\.)+", w) else w for w in words]
    words = [w for w in words if w]
    if not words:
        return None, "nothing left to say"
    out = "-".join(words)
    if len(out) > MAX_PHRASE:
        return None, f"longer than {MAX_PHRASE} characters"
    return out, ""


def build_vocabulary(phrases: list[str]) -> dict:
    """{phrases: [as sent to AWS], skipped: [{phrase, why}], respell: {"engine sixty": "Engine 60"}}."""
    out, skipped, seen, respell_map = [], [], set(), {}
    for p in phrases:
        p = " ".join(str(p).split())
        if not p:
            continue
        a, why = to_aws(p)
        if a is None:
            skipped.append({"phrase": p, "why": why})
            continue
        if a.lower() in seen:
            continue
        if len(out) >= MAX_PHRASES:
            skipped.append({"phrase": p, "why": f"over {MAX_PHRASES} phrases"})
            continue
        seen.add(a.lower())
        out.append(a)
        if re.search(r"\d", p):
            respell_map[a.replace("-", " ").lower()] = p
    return {"phrases": out, "skipped": skipped, "respell": respell_map}


def respell(text: str, mapping: dict[str, str]) -> str:
    """Spelled-out vocabulary words back to how they're written ("Engine sixty" -> "Engine 60")."""
    for said, written in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"(?<![A-Za-z])" + r"[\s-]+".join(re.escape(w) for w in said.split()) + r"(?![A-Za-z])",
                      written, text, flags=re.I)
    return text


STREET_WORDS = {"St": "Street", "Ave": "Avenue", "Rd": "Road", "Dr": "Drive", "Ln": "Lane", "Pl": "Place", "Ct": "Court",
                "Ter": "Terrace", "Blvd": "Boulevard", "Cres": "Crescent", "Pkwy": "Parkway", "Hwy": "Highway",
                "Cir": "Circle", "Tpke": "Turnpike", "Sq": "Square", "Mt": "Mount", "Ext": "Extension"}
STREET_DIRS = {"N": "North", "S": "South", "E": "East", "W": "West"}


def street_spoken(name: str) -> str:
    """'N Main St' -> 'North Main Street'."""
    words = name.split()
    out = []
    for i, w in enumerate(words):
        if w in STREET_DIRS and (i == 0 or i == len(words) - 1):
            out.append(STREET_DIRS[w])
        else:
            out.append(STREET_WORDS.get(w, w))
    return " ".join(out)


class Vocabulary:
    """The custom vocabulary on AWS (CreateVocabulary / UpdateVocabulary / GetVocabulary), its state
    kept through get/put (the CAD's meta table): {state: none | PENDING | READY | FAILED | error,
    reason, phrases, updated, checked}."""

    KEY = "aws_vocabulary"

    def __init__(self, region: str, get: Callable[[str], str | None], put: Callable[[str, str], None],
                 name: str = VOCABULARY, endpoint: str = "", call: Callable[..., dict] = aws.json_call,
                 log: Callable[[str], None] = print, poll_s: float = 10.0, on_ready: Callable[[str], None] = lambda n: None):
        self.region, self.name, self.endpoint, self.call, self.log = region, name, endpoint, call, log
        self._get, self._put, self.poll_s, self.on_ready = get, put, poll_s, on_ready
        self._lock = threading.Lock()
        self._polling = False

    def state(self) -> dict:
        try:
            d = json.loads(self._get(self.KEY) or "{}")
        except ValueError:
            d = {}
        return {"name": self.name, "state": d.get("state") or "none", "reason": d.get("reason") or "",
                "phrases": int(d.get("phrases") or 0), "updated": d.get("updated"), "checked": d.get("checked"),
                "respell": d.get("respell") or {}, "sig": d.get("sig") or ""}

    def _save(self, **kw) -> dict:
        with self._lock:
            d = self.state()
            d.update(kw)
            d.pop("name", None)
            self._put(self.KEY, json.dumps(d))
        return self.state()

    def ready(self) -> bool:
        return self.state()["state"] == "READY"

    def _api(self, op: str, body: dict) -> dict:
        return self.call(SERVICE, f"Transcribe.{op}", body, self.region, self.endpoint)

    def push(self, phrases: list[str]) -> dict:
        """Send the list (as build_vocabulary() makes it): Update when it exists, else Create; then
        GetVocabulary every poll_s until READY or FAILED (in the background)."""
        built = build_vocabulary(phrases)
        if not built["phrases"]:
            return self._save(state="error", reason="no phrases to send")
        body = {"VocabularyName": self.name, "LanguageCode": "en-US", "Phrases": built["phrases"]}
        try:
            try:
                self._api("GetVocabulary", {"VocabularyName": self.name})
                exists = True
            except aws.AwsError as e:
                if e.code not in ("NotFoundException", "BadRequestException") and e.status != 400:
                    raise
                exists = False
            r = self._api("UpdateVocabulary" if exists else "CreateVocabulary", body)
        except aws.AwsError as e:
            self.log(f"aws transcribe: vocabulary {self.name}: {e}")
            return self._save(state="error", reason=str(e)[:300], checked=time.time())
        st = self._save(state=str(r.get("VocabularyState") or "PENDING"), reason=str(r.get("FailureReason") or ""),
                        phrases=len(built["phrases"]), updated=time.time(), checked=time.time(), respell=built["respell"],
                        sig=hashlib.sha1(json.dumps(built["phrases"]).encode()).hexdigest()[:12])
        self.log(f"aws transcribe: vocabulary {self.name}: {len(built['phrases'])} phrases sent ({st['state']})")
        self.poll()
        return st

    def check(self) -> dict:
        try:
            r = self._api("GetVocabulary", {"VocabularyName": self.name})
        except aws.AwsError as e:
            if e.code == "NotFoundException" or (e.status == 400 and "not" in str(e).lower() and "found" in str(e).lower()):
                return self._save(state="none", reason="", checked=time.time())
            return self._save(state="error", reason=str(e)[:300], checked=time.time())
        st = self._save(state=str(r.get("VocabularyState") or "PENDING"), reason=str(r.get("FailureReason") or ""),
                        checked=time.time())
        if st["state"] == "READY":
            self.on_ready(self.name)
        return st

    def poll(self) -> None:
        """GetVocabulary until it isn't PENDING (in a thread; one at a time)."""
        with self._lock:
            if self._polling:
                return
            self._polling = True

        def run():
            try:
                for _ in range(360):                    # an hour at 10 s
                    st = self.check()
                    if st["state"] != "PENDING":
                        self.log(f"aws transcribe: vocabulary {self.name}: {st['state']}"
                                 + (f" ({st['reason']})" if st["reason"] else ""))
                        return
                    time.sleep(self.poll_s)
            finally:
                with self._lock:
                    self._polling = False
        threading.Thread(target=run, name="aws-vocabulary", daemon=True).start()


def pcm8k(audio: np.ndarray, rate: int) -> np.ndarray:
    """float [-1, 1] or int16 audio at 16 or 8 kHz -> int16 at 8 kHz (already band-limited to 3.7 kHz:
    every other sample, as the station's clip_8k)."""
    a = np.asarray(audio)
    if a.dtype != np.int16:
        a = (np.clip(a.astype(np.float32), -1, 1) * 32767).astype(np.int16)
    if rate == 16000:
        a = a[::2]
    elif rate != RATE:
        raise ValueError(f"audio at {rate} Hz: 8 or 16 kHz only")
    return np.ascontiguousarray(a)


def wav_pcm(wav: bytes, skip_s: float = 0.0) -> np.ndarray | None:
    """A mono 16-bit WAV (a clip) -> int16 at 8 kHz, from skip_s on. None if it isn't one."""
    import io
    import wave
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return None
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    except (wave.Error, EOFError):
        return None
    pcm = pcm[int(skip_s * rate):]
    try:
        return pcm8k(pcm, rate)
    except ValueError:
        return None
