"""AWS plumbing shared by everything sizeup calls on AWS (stdlib only, no boto3): credentials,
Signature Version 4 (headers and presigned URLs) and the JSON APIs.

    credentials()                    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN when set;
                                     else the ECS container credentials endpoint (the Fargate task role:
                                     AWS_CONTAINER_CREDENTIALS_RELATIVE_URI, or _FULL_URI with
                                     AWS_CONTAINER_AUTHORIZATION_TOKEN[_FILE]), cached and fetched again 5
                                     minutes before they expire (forget_credentials() after a 403)
    region(configured)               the configured one, else AWS_REGION / AWS_DEFAULT_REGION, else us-east-1
    sign(method, url, headers, body, creds, region, service)
                                     the request's headers plus Host, X-Amz-Date, X-Amz-Security-Token and
                                     Authorization (SigV4, every given header signed)
    presign(url, creds, region, service, expires)
                                     the URL with SigV4 query authentication (X-Amz-Algorithm ... X-Amz-Signature),
                                     e.g. the Transcribe streaming WebSocket URL
    json_call(service, target, body, region, endpoint)
                                     one call to an "application/x-amz-json-1.1" API (Transcribe's
                                     CreateVocabulary, Polly-style JSON APIs): the decoded answer, or AwsError
                                     with AWS's own reason ("AccessDeniedException: ...")

Written for Amazon Transcribe (sizeup/transcribe.py) so that the Polly voice (sizeup/polly.py on its
branch: the same Credentials / credentials() / sign() shapes) can import it instead of its own copy:
`from sizeup.aws import Credentials, credentials, forget_credentials, region, sign`.
"""

from __future__ import annotations

import hashlib
import hmac
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

DEFAULT_REGION = "us-east-1"
CREDS_S = 3.0               # the container credentials endpoint
REFRESH_BEFORE_S = 300.0    # fetch new container credentials this long before they expire
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
ALGORITHM = "AWS4-HMAC-SHA256"


class AwsError(RuntimeError):
    """AWS couldn't do it (no credentials, no permission, network, AWS's own error). code: AWS's error
    type ("AccessDeniedException", "NoCredentials", "HTTP 503"), status: the HTTP status (0: none)."""

    def __init__(self, msg: str, code: str = "", status: int = 0):
        super().__init__(msg)
        self.code, self.status = code, status


def region(configured: str = "") -> str:
    return (configured or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION).strip()


# -- credentials ---------------------------------------------------------------------------------------

class Credentials:
    __slots__ = ("access_key", "secret_key", "token", "expires")

    def __init__(self, access_key: str, secret_key: str, token: str = "", expires: float | None = None):
        self.access_key, self.secret_key, self.token, self.expires = access_key, secret_key, token, expires

    def __repr__(self) -> str:            # never the secret
        return f"Credentials({self.access_key[:4]}…, token={'yes' if self.token else 'no'}, expires={self.expires})"


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
            raise AwsError("AWS_CONTAINER_CREDENTIALS_FULL_URI: http only to the container's own endpoint", "NoCredentials")
    token = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN", "").strip()
    tfile = os.environ.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", "").strip()
    if tfile and not token:
        try:
            with open(tfile) as f:
                token = f.read().strip()
        except OSError as e:
            raise AwsError(f"AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE: {e}", "NoCredentials") from e
    return full, ({"Authorization": token} if token else {})


def _expiry(s) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def credentials(now: Callable[[], float] = time.time) -> Credentials:
    """The credentials to sign with (see the module doc). AwsError("NoCredentials") when there are none."""
    global _creds
    ak, sk = os.environ.get("AWS_ACCESS_KEY_ID", "").strip(), os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if ak and sk:
        return Credentials(ak, sk, os.environ.get("AWS_SESSION_TOKEN", "").strip())
    uri, headers = _container_uri()
    if not uri:
        raise AwsError("no AWS credentials (AWS_ACCESS_KEY_ID or the ECS task role)", "NoCredentials")
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
            raise AwsError(f"container credentials: HTTP {e.code}", "NoCredentials") from e
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise AwsError(f"container credentials: {getattr(e, 'reason', e)}", "NoCredentials") from e
        _creds = c
        return c


def forget_credentials() -> None:
    """Drop the cached container credentials (after AWS refused them): the next call fetches new ones."""
    global _creds
    with _creds_lock:
        _creds = None


# -- Signature Version 4 ----------------------------------------------------------------------------------

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _quote(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def _stamp(at: datetime | None) -> tuple[str, str]:
    at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return at.strftime("%Y%m%dT%H%M%SZ"), at.strftime("%Y%m%d")


def _canonical_path(path: str) -> str:
    return "/".join(_quote(urllib.parse.unquote(p)) for p in (path or "/").split("/")) or "/"


def _canonical_query(pairs: list[tuple[str, str]]) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted((_quote(k), _quote(v)) for k, v in pairs))


def _signature(secret: str, day: str, reg: str, service: str, amz: str, creq: str) -> str:
    scope = f"{day}/{reg}/{service}/aws4_request"
    sts = "\n".join([ALGORITHM, amz, scope, hashlib.sha256(creq.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret).encode(), day), reg), service), "aws4_request")
    return hmac.new(key, sts.encode(), hashlib.sha256).hexdigest()


def sign(method: str, url: str, headers: dict[str, str], body: bytes, creds: Credentials, reg: str,
         service: str, at: datetime | None = None) -> dict[str, str]:
    """The request's headers plus Host, X-Amz-Date, X-Amz-Security-Token (temporary credentials) and
    Authorization (AWS Signature Version 4). Every header given is signed."""
    amz, day = _stamp(at)
    u = urllib.parse.urlsplit(url)
    out = dict(headers)
    out["Host"] = u.netloc
    out["X-Amz-Date"] = amz
    if creds.token:
        out["X-Amz-Security-Token"] = creds.token
    canon = {k.lower().strip(): re.sub(r"\s+", " ", str(v).strip()) for k, v in out.items()}
    names = sorted(canon)
    creq = "\n".join([method.upper(), _canonical_path(u.path),
                      _canonical_query(urllib.parse.parse_qsl(u.query, keep_blank_values=True)),
                      "".join(f"{k}:{canon[k]}\n" for k in names), ";".join(names), hashlib.sha256(body).hexdigest()])
    sig = _signature(creds.secret_key, day, reg, service, amz, creq)
    out["Authorization"] = (f"{ALGORITHM} Credential={creds.access_key}/{day}/{reg}/{service}/aws4_request, "
                            f"SignedHeaders={';'.join(names)}, Signature={sig}")
    return out


def presign(url: str, creds: Credentials, reg: str, service: str, expires: int = 300, at: datetime | None = None,
            payload_hash: str = EMPTY_SHA256, method: str = "GET") -> str:
    """url with SigV4 query authentication (only `host` signed). The session token goes in the signed
    query (X-Amz-Security-Token), as Transcribe's WebSocket API and S3 want it."""
    amz, day = _stamp(at)
    u = urllib.parse.urlsplit(url)
    pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    pairs += [("X-Amz-Algorithm", ALGORITHM), ("X-Amz-Credential", f"{creds.access_key}/{day}/{reg}/{service}/aws4_request"),
              ("X-Amz-Date", amz), ("X-Amz-Expires", str(int(expires))), ("X-Amz-SignedHeaders", "host")]
    if creds.token:
        pairs.append(("X-Amz-Security-Token", creds.token))
    query = _canonical_query(pairs)
    creq = "\n".join([method.upper(), _canonical_path(u.path), query, f"host:{u.netloc}\n", "host", payload_hash])
    sig = _signature(creds.secret_key, day, reg, service, amz, creq)
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path or "/", f"{query}&X-Amz-Signature={sig}", ""))


# -- JSON APIs ------------------------------------------------------------------------------------------

def error_from(status: int, headers, data: bytes) -> AwsError:
    """AWS's error answer as 'AccessDeniedException: User ... is not authorized ...'."""
    code = ((headers.get("x-amzn-ErrorType") if headers is not None else "") or "").split(":")[0].strip()
    msg = ""
    try:
        d = json.loads(data or b"{}")
        code = code or str(d.get("__type") or d.get("code") or "").split("#")[-1]
        msg = str(d.get("message") or d.get("Message") or "")
    except (ValueError, AttributeError):
        msg = (data or b"").decode(errors="replace")[:200]
    code = code or f"HTTP {status}"
    return AwsError(f"{code}: {msg}" if msg else code, code, status)


def json_call(service: str, target: str, body: dict, reg: str, endpoint: str = "", timeout: float = 15.0,
              creds: Credentials | None = None) -> dict:
    """One POST to an x-amz-json-1.1 API, e.g. json_call("transcribe", "Transcribe.GetVocabulary",
    {"VocabularyName": "sizeup-pcfd"}, "us-east-1"). endpoint: another base URL (tests)."""
    url = (endpoint or f"https://{service}.{reg}.amazonaws.com").rstrip("/") + "/"
    data = json.dumps(body, separators=(",", ":")).encode()
    c = creds or credentials()
    hdrs = sign("POST", url, {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": target}, data, c, reg, service)
    req = urllib.request.Request(url, data=data, method="POST", headers={k: v for k, v in hdrs.items() if k != "Host"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        err = error_from(e.code, e.headers, e.read())
        if e.code == 403 and creds is None:
            forget_credentials()
        raise err from None
    except OSError as e:
        raise AwsError(f"{service}: {getattr(e, 'reason', e)}", "Network") from None
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        raise AwsError(f"{service}: an answer that isn't JSON", "BadAnswer") from None
    return out if isinstance(out, dict) else {}
