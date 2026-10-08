"""The dispatch voice from Amazon Polly, server side (the hub: [dispatch] tts_engine = "polly").
Stdlib only: SynthesizeSpeech over HTTPS, signed with AWS Signature Version 4 here (no boto3).

    synthesize(text, "Matthew", speed)  -> float32 samples, 16 kHz mono (Polly's "pcm" is signed
                                           16-bit little-endian), or PollyError with Polly's reason
                                           ("AccessDeniedException: User ... is not authorized ...")
    VOICES                                 the US English voices offered, males first, with the engines
                                           each one has; a voice is tried on its best engine first
                                           (generative, then neural, then standard) and the one that
                                           worked is remembered (engine_in_use)
    credentials()                          AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
                                           when set; else the ECS container credentials endpoint (the
                                           task role: AWS_CONTAINER_CREDENTIALS_RELATIVE_URI, or _FULL_URI
                                           with AWS_CONTAINER_AUTHORIZATION_TOKEN[_FILE]), cached until
                                           5 minutes before they expire
    sign(...)                              SigV4 headers for one request

Region: [dispatch] polly_region, else AWS_REGION / AWS_DEFAULT_REGION (the Fargate task has it), else
us-east-1. SIZEUP_POLLY_ENDPOINT (e.g. "http://127.0.0.1:9000") points it somewhere else (tests).

Speed: the dispatch speed (1.0 = the voice's own pace) is said as SSML <prosody rate="92%">; if Polly
rejects the SSML for an engine, that engine is used with plain text at its own pace. Text longer than
MAX_CHARS is split at sentence ends and the pieces joined (Polly bills and caps 3000 characters a request).
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable
from xml.sax.saxutils import escape

import numpy as np

RATE = 16000
CONNECT_S = 3.0
READ_S = 10.0
CREDS_S = 3.0             # the container credentials endpoint
REFRESH_BEFORE_S = 300.0  # fetch new container credentials this long before they expire
MAX_CHARS = 2900          # under Polly's 3000 billed characters a request
DEFAULT_REGION = "us-east-1"
DEFAULT_VOICE = "Matthew"
ENGINE_ORDER = ("generative", "neural", "standard")

# US English voices and the engines Amazon lists for each (best first). Males first: the dispatch voice
# is a man's (user, 2026-10-08). Availability per region can differ; a voice/engine pair Polly rejects
# is skipped for the next engine, so an entry here that's off by one engine costs one request, once.
VOICES: dict[str, dict] = {
    "Matthew": {"gender": "male", "engines": ("generative", "neural", "standard")},
    "Stephen": {"gender": "male", "engines": ("generative", "neural")},
    "Gregory": {"gender": "male", "engines": ("neural",)},
    "Joey": {"gender": "male", "engines": ("neural", "standard")},
    "Joanna": {"gender": "female", "engines": ("generative", "neural", "standard")},
    "Ruth": {"gender": "female", "engines": ("generative", "neural")},
    "Danielle": {"gender": "female", "engines": ("generative", "neural")},
}
STATION_VOICE = "am_michael"   # the station's Kokoro voice when the hub falls back to it (a man's too)

# Polly errors that mean "not this engine (or not this SSML) for this voice": try the next one.
ENGINE_ERRORS = ("EngineNotSupportedException", "ValidationException", "InvalidSsmlException")


class PollyError(RuntimeError):
    """Polly couldn't say it (no credentials, no permission, network, Polly's own error)."""

    def __init__(self, msg: str, code: str = "", status: int = 0):
        super().__init__(msg)
        self.code, self.status = code, status


def voice_info(v: str) -> dict:
    """'Matthew' -> {id, name, accent, gender, engines: [...]} (the Settings voice list)."""
    d = VOICES.get(v, {})
    return {"id": v, "name": v, "accent": "American", "gender": d.get("gender", ""),
            "engines": list(d.get("engines", ENGINE_ORDER))}


def voices() -> list[dict]:
    return [voice_info(v) for v in VOICES]


def region(configured: str = "") -> str:
    return (configured or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION).strip()


def endpoint(reg: str) -> str:
    return (os.environ.get("SIZEUP_POLLY_ENDPOINT") or f"https://polly.{reg}.amazonaws.com").rstrip("/")


# -- credentials ---------------------------------------------------------------------------------------

class Credentials:
    __slots__ = ("access_key", "secret_key", "token", "expires")

    def __init__(self, access_key: str, secret_key: str, token: str = "", expires: float | None = None):
        self.access_key, self.secret_key, self.token, self.expires = access_key, secret_key, token, expires


_creds_lock = threading.Lock()
_creds: Credentials | None = None


def _container_uri() -> tuple[str, dict]:
    rel = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "").strip()
    if rel:
        return "http://169.254.170.2" + (rel if rel.startswith("/") else "/" + rel), {}
    full = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI", "").strip()
    if not full:
        return "", {}
    u = urllib.parse.urlsplit(full)
    if u.scheme != "https":
        try:
            ok = u.scheme == "http" and (ipaddress.ip_address(u.hostname or "").is_loopback
                                         or u.hostname in ("169.254.170.2", "169.254.170.23", "fd00:ec2::23"))
        except ValueError:
            ok = u.scheme == "http" and u.hostname == "localhost"
        if not ok:
            raise PollyError("AWS_CONTAINER_CREDENTIALS_FULL_URI: http only to the container's own endpoint")
    token = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN", "").strip()
    tfile = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "").strip()
    if tfile and not token:
        try:
            token = open(tfile).read().strip()
        except OSError as e:
            raise PollyError(f"AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: {e}") from e
    return full, ({"Authorization": token} if token else {})


def _expiry(s) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def credentials(now: Callable[[], float] = time.time) -> Credentials:
    """The credentials to sign with (see the module doc). PollyError when there are none."""
    global _creds
    ak, sk = os.environ.get("AWS_ACCESS_KEY_ID", "").strip(), os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if ak and sk:
        return Credentials(ak, sk, os.environ.get("AWS_SESSION_TOKEN", "").strip())
    uri, headers = _container_uri()
    if not uri:
        raise PollyError("no AWS credentials (AWS_ACCESS_KEY_ID or the ECS task role)", "NoCredentials")
    with _creds_lock:
        c = _creds
        if c is not None and (c.expires is None or now() < c.expires - REFRESH_BEFORE_S):
            return c
        req = urllib.request.Request(uri, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=CREDS_S) as r:
                d = json.loads(r.read())
            c = Credentials(str(d["AccessKeyId"]), str(d["SecretAccessKey"]), str(d.get("Token") or ""),
                            _expiry(d.get("Expiration")))
        except urllib.error.HTTPError as e:
            raise PollyError(f"container credentials: HTTP {e.code}", "NoCredentials") from e
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise PollyError(f"container credentials: {getattr(e, 'reason', e)}", "NoCredentials") from e
        _creds = c
        return c


def forget_credentials() -> None:
    global _creds
    with _creds_lock:
        _creds = None


# -- SigV4 ---------------------------------------------------------------------------------------------

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _quote(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def sign(method: str, url: str, headers: dict[str, str], body: bytes, creds: Credentials, reg: str,
         service: str = "polly", at: datetime | None = None) -> dict[str, str]:
    """The request's headers plus Host, X-Amz-Date, X-Amz-Security-Token (temporary credentials) and
    Authorization (AWS Signature Version 4). Every header given is signed."""
    at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz, day = at.strftime("%Y%m%dT%H%M%SZ"), at.strftime("%Y%m%d")
    u = urllib.parse.urlsplit(url)
    out = dict(headers)
    out["Host"] = u.netloc
    out["X-Amz-Date"] = amz
    if creds.token:
        out["X-Amz-Security-Token"] = creds.token
    canon = {}
    for k, v in out.items():
        canon[k.lower().strip()] = re.sub(r"\s+", " ", str(v).strip())
    names = sorted(canon)
    query = sorted((_quote(k), _quote(v)) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    path = "/".join(_quote(urllib.parse.unquote(p)) for p in (u.path or "/").split("/")) or "/"
    creq = "\n".join([method.upper(), path, "&".join(f"{k}={v}" for k, v in query),
                      "".join(f"{k}:{canon[k]}\n" for k in names), ";".join(names), hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{reg}/{service}/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + creds.secret_key).encode(), day), reg), service), "aws4_request")
    sig = hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds.access_key}/{scope}, "
                            f"SignedHeaders={';'.join(names)}, Signature={sig}")
    return out


# -- SynthesizeSpeech ----------------------------------------------------------------------------------

def _error(status: int, headers, data: bytes) -> PollyError:
    """Polly's error: 'AccessDeniedException: User: ... is not authorized ...'."""
    code = (headers.get("x-amzn-ErrorType") or "").split(":")[0].strip()
    msg = ""
    try:
        d = json.loads(data or b"{}")
        code = code or str(d.get("__type") or d.get("code") or "").split("#")[-1]
        msg = str(d.get("message") or d.get("Message") or "")
    except (ValueError, AttributeError):
        msg = (data or b"").decode(errors="replace")[:200]
    code = code or f"HTTP {status}"
    return PollyError(f"{code}: {msg}" if msg else code, code, status)


def request(text: str, voice: str, engine: str, ssml: bool, reg: str) -> bytes:
    """One SynthesizeSpeech call: the raw PCM (16-bit LE, 16 kHz mono). PollyError."""
    url = endpoint(reg) + "/v1/speech"
    body = json.dumps({"Engine": engine, "OutputFormat": "pcm", "SampleRate": str(RATE), "Text": text,
                       "TextType": "ssml" if ssml else "text", "VoiceId": voice}).encode()
    headers = sign("POST", url, {"Content-Type": "application/json"}, body, credentials(), reg)
    u = urllib.parse.urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.hostname, u.port, timeout=CONNECT_S)
    try:
        try:
            conn.connect()
        except (TimeoutError, OSError) as e:
            raise PollyError(f"can't reach {u.hostname}: {e}", "Network") from e
        conn.sock.settimeout(READ_S)
        try:
            conn.request("POST", u.path, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
        except TimeoutError as e:
            raise PollyError(f"no answer in {READ_S:g} s", "Timeout") from e
        except (OSError, http.client.HTTPException) as e:
            raise PollyError(f"{u.hostname}: {e}", "Network") from e
        if r.status != 200:
            if r.status == 403 and "ExpiredToken" in (r.getheader("x-amzn-ErrorType") or "") + data.decode(errors="replace"):
                forget_credentials()
            raise _error(r.status, r.headers, data)
        return data
    finally:
        conn.close()


def chunks(text: str, limit: int = MAX_CHARS) -> list[str]:
    """Text in pieces of at most `limit` characters, split at sentence ends (else at a space)."""
    text = re.sub(r"\s+", " ", text).strip()
    out, cur = [], ""
    for s in re.split(r"(?<=[.!?])\s+", text):
        while len(s) > limit:                                   # one huge sentence: at a space
            cut = s.rfind(" ", 0, limit + 1)
            cut = cut if cut > 0 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return [c for c in out if c]


def _ssml(text: str, speed: float) -> str:
    return f'<speak><prosody rate="{round(speed * 100)}%">{escape(text)}</prosody></speak>'


# (region, voice) -> (engine, ssml works) that last worked: the next request starts there.
_works: dict[tuple[str, str], tuple[str, bool]] = {}


def engine_in_use(voice: str, configured_region: str = "") -> str:
    """The engine this voice last spoke with ('' before its first request)."""
    return _works.get((region(configured_region), voice), ("", True))[0]


def _tries(voice: str, speed: float, known: tuple[str, bool] | None) -> list[tuple[str, bool]]:
    """(engine, SSML?) in the order to try: from the engine that last worked (else the voice's best);
    with a speed to say, SSML first and the same engine in plain text next (unless SSML is known bad there)."""
    engines = list(VOICES.get(voice, {}).get("engines", ENGINE_ORDER))
    if known and known[0] in engines:
        engines = engines[engines.index(known[0]):]
    paced = abs(speed - 1.0) > 0.005
    out = []
    for e in engines:
        if paced and not (known and known == (e, False)):
            out.append((e, True))
        out.append((e, False))
    return out


def _say(text: str, voice: str, speed: float, reg: str, log: Callable[[str], None] | None) -> bytes:
    known = _works.get((reg, voice))
    last: PollyError | None = None
    for engine, ssml in _tries(voice, speed, known):
        try:
            pcm = request(_ssml(text, speed) if ssml else text, voice, engine, ssml, reg)
        except PollyError as e:
            if e.status == 400 and e.code in ENGINE_ERRORS:
                last = e                                        # not this engine (or SSML) for this voice: the next
                continue
            raise
        works = (engine, ssml or abs(speed - 1.0) <= 0.005)
        if known != works:
            _works[(reg, voice)] = works
            if last is not None and log:
                log(f"Polly: {voice} speaks with the {engine} engine"
                    f"{'' if works[1] else ' at its own pace'} ({last})")
        return pcm
    raise last or PollyError(f"{voice}: no engine to try")


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0, configured_region: str = "",
               log: Callable[[str], None] | None = None) -> np.ndarray:
    """The text said by Polly, as float32 samples at 16 kHz. Raises PollyError."""
    if not text.strip():
        return np.zeros(0, np.float32)
    reg = region(configured_region)
    parts = [_say(c, voice, speed, reg, log) for c in chunks(text)]
    pcm = b"".join(p[: len(p) - len(p) % 2] for p in parts)
    if not pcm:
        raise PollyError("Polly sent no audio", "NoAudio")
    return np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0
