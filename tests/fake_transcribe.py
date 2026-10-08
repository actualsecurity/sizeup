"""A local stand-in for Amazon Transcribe (tests and the dev instance: never the real AWS).

FakeTranscribe: the streaming WebSocket API. It takes the presigned GET, answers 101, reads masked
binary frames carrying event-stream AudioEvents (8 kHz PCM), and "hears" each tone burst in the audio
(a run of samples over the threshold, ended by >= 0.2 s of silence) as the phrase of the nearest
frequency in `phrases`. Per burst it sends a partial TranscriptEvent, then a final one with the words
spread over the burst's time in the stream (StartTime / EndTime, Items with Confidence). merge=True
holds each final until the next burst and sends the two as one result (to check that words are split
between transmissions by time). An empty AudioEvent ends the stream: what is open is finalized and the
connection closed. fail_after_s: a ServiceUnavailableException once that much audio came in.
refuse: answer the handshake with that HTTP status instead.

FakeTranscribeApi: the JSON API (Transcribe.CreateVocabulary / UpdateVocabulary / GetVocabulary):
the vocabulary is PENDING for `pending_polls` GetVocabulary calls, then READY.

    python -m tests.fake_transcribe 9431 9432      # both, for a dev instance ([stt] aws_endpoint / aws_api_endpoint)
"""

from __future__ import annotations

import base64
import hashlib
import json
import socketserver
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from sizeup.transcribe import (OP_BINARY, OP_CLOSE, OP_PING, OP_PONG, RATE, WS_GUID, EventStreamError, decode_message,
                               encode_message)

DEFAULT_PHRASES = {400: "Engine 60 on location nothing showing", 600: "Ladder 31 responding",
                   800: "60 Control copy", 1000: "Port Chester Fire respond to an activated fire alarm"}


def _event(results: list[dict]) -> bytes:
    return encode_message({":event-type": "TranscriptEvent", ":content-type": "application/json", ":message-type": "event"},
                          json.dumps({"Transcript": {"Results": results}}).encode())


def _exception(kind: str, msg: str) -> bytes:
    return encode_message({":exception-type": kind, ":content-type": "application/json", ":message-type": "exception"},
                          json.dumps({"Message": msg}).encode())


class Conn:
    """One stream as the fake saw it."""

    def __init__(self, query: dict):
        self.query = query
        self.audio_s = 0.0
        self.bursts: list[tuple[float, float, str]] = []
        self.ended = False
        self.closed_at: float | None = None


class FakeTranscribe:
    def __init__(self, phrases: dict | None = None, merge: bool = False, fail_after_s: float | None = None,
                 refuse: int = 0, threshold: int = 200, port: int = 0):
        self.phrases = dict(phrases or DEFAULT_PHRASES)
        self.merge, self.fail_after_s, self.refuse, self.threshold = merge, fail_after_s, refuse, threshold
        self.conns: list[Conn] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                fake._serve(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.url = f"ws://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # -- the wire ----------------------------------------------------------------------------------------

    @staticmethod
    def _send(sock, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                                         else bytes([127]) + struct.pack(">Q", n))
        sock.sendall(head + payload)

    @staticmethod
    def _recv_frame(sock, buf: bytearray):
        def need(k):
            while len(buf) < k:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("closed")
                buf.extend(chunk)
        need(2)
        op, ln, i = buf[0] & 0x0F, buf[1] & 0x7F, 2
        assert buf[1] & 0x80, "a client frame must be masked"
        if ln == 126:
            need(4)
            ln, i = struct.unpack(">H", bytes(buf[2:4]))[0], 4
        elif ln == 127:
            need(10)
            ln, i = struct.unpack(">Q", bytes(buf[2:10]))[0], 10
        need(i + 4 + ln)
        key = bytes(buf[i:i + 4])
        data = np.frombuffer(bytes(buf[i + 4:i + 4 + ln]), np.uint8)
        k = np.frombuffer((key * (ln // 4 + 1))[:ln], np.uint8)
        del buf[:i + 4 + ln]
        return op, (data ^ k).tobytes()

    def _serve(self, sock) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                return
            data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode()
        line, *hdrs = head.split("\r\n")
        target = line.split(" ")[1]
        u = urllib.parse.urlsplit(target)
        query = dict(urllib.parse.parse_qsl(u.query))
        h = {k.strip().lower(): v.strip() for k, _, v in (x.partition(":") for x in hdrs)}
        missing = [k for k in ("language-code", "media-encoding", "sample-rate", "X-Amz-Algorithm", "X-Amz-Credential",
                               "X-Amz-Date", "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature") if k not in query]
        status = self.refuse or (400 if missing or u.path != "/stream-transcription-websocket" else 0)
        if status:
            body = json.dumps({"message": "refused" if self.refuse else f"missing {missing}"}).encode()
            sock.sendall(f"HTTP/1.1 {status} Refused\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            return
        accept = base64.b64encode(hashlib.sha1((h["sec-websocket-key"] + WS_GUID).encode()).digest()).decode()
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        conn = Conn(query)
        with self.lock:
            self.conns.append(conn)
        rate = int(query.get("sample-rate") or RATE)
        st = {"pcm": [], "n": 0, "burst": None, "quiet": 0, "held": [], "rid": 0}
        buf = bytearray()
        try:
            while True:
                op, payload = self._recv_frame(sock, buf)
                if op == OP_CLOSE:
                    self._send(sock, OP_CLOSE, payload[:2])
                    return
                if op == OP_PING:
                    self._send(sock, OP_PONG, payload)
                    continue
                if op != OP_BINARY:
                    continue
                try:
                    headers, audio, _ = decode_message(payload)
                except EventStreamError as e:
                    self._send(sock, OP_BINARY, _exception("BadRequestException", f"bad event: {e}"))
                    return
                assert headers.get(":event-type") == "AudioEvent" and headers.get(":message-type") == "event"
                if not audio:                                   # end of stream
                    conn.ended = True
                    self._close_burst(sock, st, rate, final=True)
                    self._flush_held(sock, st)
                    self._send(sock, OP_CLOSE, struct.pack(">H", 1000))
                    return
                self._audio(sock, conn, st, np.frombuffer(audio, "<i2"), rate)
                if self.fail_after_s is not None and conn.audio_s >= self.fail_after_s:
                    self._send(sock, OP_BINARY, _exception("ServiceUnavailableException", "Service unavailable (fake)"))
                    self._send(sock, OP_CLOSE, struct.pack(">H", 1011))
                    return
        except (ConnectionError, OSError, AssertionError):
            return
        finally:
            conn.closed_at = time.time()
            try:
                sock.close()
            except OSError:
                pass

    # -- hearing -----------------------------------------------------------------------------------------

    def _audio(self, sock, conn: Conn, st: dict, pcm: np.ndarray, rate: int) -> None:
        loud = np.abs(pcm.astype(np.int32)) > self.threshold
        for k, x in enumerate(loud):
            i = st["n"] + k
            if x:
                if st["burst"] is None:
                    st["burst"] = [i, i]
                    st["pcm"] = []
                    self._partial(sock, st, i / rate)
                st["burst"][1] = i + 1
                st["quiet"] = 0
            elif st["burst"] is not None:
                st["quiet"] += 1
                if st["quiet"] >= int(0.2 * rate):
                    self._close_burst(sock, st, rate)
            if st["burst"] is not None:
                st["pcm"].append(int(pcm[k]))
        st["n"] += len(pcm)
        conn.audio_s = st["n"] / rate
        conn.bursts = st.setdefault("all", [])

    def _partial(self, sock, st: dict, at: float) -> None:
        st["rid"] += 1
        self._send(sock, OP_BINARY, _event([{"ResultId": f"r{st['rid']}", "IsPartial": True, "StartTime": at,
                                             "EndTime": at + 0.1, "Alternatives": [{"Transcript": "...", "Items": []}]}]))

    def _phrase(self, pcm: list[int], rate: int) -> str:
        a = np.asarray(pcm, np.float64)
        crossings = np.count_nonzero(np.diff(np.signbit(a)))
        hz = crossings / 2 / max(len(a) / rate, 1e-3)
        best = min(self.phrases, key=lambda f: abs(f - hz))
        return self.phrases[best]

    def _close_burst(self, sock, st: dict, rate: int, final: bool = False) -> None:
        b = st["burst"]
        if b is None:
            return
        st["burst"] = None
        t0, t1 = b[0] / rate, b[1] / rate
        text = self._phrase(st["pcm"][:b[1] - b[0]], rate)
        st.setdefault("all", []).append((t0, t1, text))
        words = text.split()
        step = (t1 - t0) / len(words)
        items = [{"Content": w, "StartTime": round(t0 + k * step, 3), "EndTime": round(t0 + (k + 1) * step, 3),
                  "Type": "pronunciation", "Confidence": 0.9} for k, w in enumerate(words)]
        items.append({"Content": ".", "StartTime": round(t1, 3), "EndTime": round(t1, 3), "Type": "punctuation"})
        res = {"ResultId": f"r{st['rid']}", "IsPartial": False, "StartTime": t0, "EndTime": t1,
               "Alternatives": [{"Transcript": text + ".", "Items": items}]}
        if self.merge and not final:
            st["held"].append(res)
            if len(st["held"]) >= 2:
                self._flush_held(sock, st)
            return
        if st["held"]:
            st["held"].append(res)
            return self._flush_held(sock, st)
        self._send(sock, OP_BINARY, _event([res]))

    def _flush_held(self, sock, st: dict) -> None:
        held, st["held"] = st["held"], []
        if not held:
            return
        items = [it for r in held for it in r["Alternatives"][0]["Items"]]
        merged = {"ResultId": held[-1]["ResultId"], "IsPartial": False, "StartTime": held[0]["StartTime"],
                  "EndTime": held[-1]["EndTime"],
                  "Alternatives": [{"Transcript": " ".join(r["Alternatives"][0]["Transcript"] for r in held), "Items": items}]}
        self._send(sock, OP_BINARY, _event([merged]))


class FakeTranscribeApi:
    """The Transcribe JSON API's vocabulary calls (x-amz-json-1.1, SigV4-signed)."""

    def __init__(self, pending_polls: int = 1, port: int = 0):
        self.vocabularies: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.pending_polls = pending_polls
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                target = self.headers.get("X-Amz-Target") or ""
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("AWS4-HMAC-SHA256 Credential=") or "/transcribe/aws4_request" not in auth:
                    return self._out(403, {"__type": "UnrecognizedClientException", "message": "not signed"})
                if self.headers.get("Content-Type") != "application/x-amz-json-1.1":
                    return self._out(400, {"__type": "SerializationException", "message": "content type"})
                op = target.removeprefix("Transcribe.")
                fake.calls.append((op, body))
                name = body.get("VocabularyName")
                v = fake.vocabularies.get(name)
                if op == "GetVocabulary":
                    if v is None:
                        return self._out(400, {"__type": "NotFoundException",
                                               "message": "The requested vocabulary couldn't be found."})
                    v["polls"] += 1
                    if v["state"] == "PENDING" and v["polls"] > fake.pending_polls:
                        v["state"] = "READY"
                    return self._out(200, {"VocabularyName": name, "LanguageCode": "en-US", "VocabularyState": v["state"]})
                if op in ("CreateVocabulary", "UpdateVocabulary"):
                    if (op == "CreateVocabulary") == (v is not None):
                        return self._out(400, {"__type": "ConflictException" if v else "BadRequestException",
                                               "message": "exists" if v else "The requested vocabulary couldn't be found."})
                    bad = [p for p in body.get("Phrases") or [] if any(c.isdigit() or c == " " for c in p)]
                    if bad:
                        return self._out(400, {"__type": "BadRequestException", "message": f"invalid phrases {bad[:3]}"})
                    fake.vocabularies[name] = {"state": "PENDING", "phrases": body["Phrases"], "polls": 0}
                    return self._out(200, {"VocabularyName": name, "LanguageCode": "en-US", "VocabularyState": "PENDING"})
                return self._out(400, {"__type": "UnknownOperationException", "message": op})

            def _out(self, code, obj):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/x-amz-json-1.1")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def tone8k(hz: float, s: float, amp: int = 8000) -> np.ndarray:
    """A test 'transmission': a tone at 8 kHz, int16."""
    return (amp * np.sin(2 * np.pi * hz * np.arange(int(RATE * s)) / RATE)).astype(np.int16)


if __name__ == "__main__":                       # a dev instance's fake AWS
    ws_port, api_port = (int(sys.argv[1]), int(sys.argv[2])) if len(sys.argv) > 2 else (9431, 9432)
    f, a = FakeTranscribe(port=ws_port), FakeTranscribeApi(port=api_port)
    print(f"fake Transcribe streaming {f.url}, JSON API {a.url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
