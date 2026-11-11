"""An adapter credential kept in AWS SSM Parameter Store, so it can be changed without touching the station.

The station's incident-source adapter (#301) reads its credentials with credential("NAME") (sizeup/procenv.py): files
in systemd's credstore, set at the station by `sudo sizeup-station adapter setup`. A credential that changes now and
then (a shared login's password) can instead live in the department's AWS account as a SecureString parameter,

    /sizeup-station/adapter/NAME          (PREFIX + the credential's name), encrypted with a KMS key of its own

which the station reads with an IAM user of its own (sizeup-station-adapter: ssm:GetParameter on that prefix and
kms:Decrypt on that key, nothing else). Its access key is itself an adapter credential in the station's credstore,
never station.env or the environment:

    SIZEUP_SSM_KEY_ID   SIZEUP_SSM_SECRET   SIZEUP_SSM_REGION      (KEY_ID, SECRET, REGION below)

The hub never holds that key, never relays a value, and its roles are denied the prefix and the key outright (the
department's setup scripts). This module only fetches; how often, what to cache and when a changed version may be
tried belong to the adapter (it keeps values in memory only).

    configured(read)            whether the station has an SSM key at all (never a value)
    get(names, read, send)      {name: Param(value, version, modified)} for each NAME that has a parameter (a missing
                                one is left out: the adapter falls back to its local value); SsmError when SSM can't
                                be asked or says no. Never raises with a value, a key or AWS's message in it.

Stdlib only (sizeup/aws.py signs, Signature Version 4); HTTPS to ssm.<region>.amazonaws.com, no proxy from the
environment.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Callable, Iterable

from sizeup import aws
from sizeup.procenv import credential

PREFIX = "/sizeup-station/adapter/"
KEY_ID, SECRET, REGION = "SIZEUP_SSM_KEY_ID", "SIZEUP_SSM_SECRET", "SIZEUP_SSM_REGION"
NAMES = (KEY_ID, SECRET, REGION)                  # the station's credstore holds these (sizeup-adapter.<NAME>)
DEFAULT_REGION = "us-east-1"
TIMEOUT_S = 8.0
TARGET = "AmazonSSM.GetParameter"

REGION_RE = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-\d{1,2}")
KEY_ID_RE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{12,124}")
SECRET_RE = re.compile(r"[A-Za-z0-9/+=]{16,128}")
PARAM_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")    # = autoupdate.ADAPTER_ENV: a credential's name

Reader = Callable[[str], str]
# send(url, headers, body, timeout) -> (HTTP status, body): urllib by default, a fake in tests
Sender = Callable[[str, dict, bytes, float], "tuple[int, bytes]"]


class SsmError(RuntimeError):
    """SSM couldn't be asked, or said no. str(): a short reason for a log or Health ("AccessDeniedException",
    "network", "HTTP 503"); code: the same, for code. Never a value, a key or AWS's own message."""

    def __init__(self, code: str, status: int = 0):
        super().__init__(code)
        self.code, self.status = code, status


class Param:
    """One parameter's value (in memory only), its version and when it was last changed (epoch seconds, 0: unknown).
    repr/str hide the value."""
    __slots__ = ("name", "value", "version", "modified")

    def __init__(self, name: str, value: str, version: int, modified: float = 0.0):
        self.name, self.value, self.version, self.modified = name, value, int(version), float(modified or 0.0)

    def __repr__(self) -> str:
        return f"Param({self.name}, v{self.version}, <hidden>)"

    __str__ = __repr__


def problem(name: str, value: str) -> str:
    """Why a value for KEY_ID / SECRET / REGION won't do ("" = it will): setup's check. Never echoes the value."""
    if name == REGION:
        return "" if REGION_RE.fullmatch(value) else "a region such as us-east-1"
    if name == KEY_ID:
        return "" if KEY_ID_RE.fullmatch(value) else "an access key id (AKIA...)"
    if name == SECRET:
        return "" if SECRET_RE.fullmatch(value) else "a secret access key (40 characters)"
    return ""


def configured(read: Reader = credential) -> bool:
    """Whether the station has an SSM key (both halves; the values are never returned)."""
    try:
        return bool(read(KEY_ID) and read(SECRET))
    except Exception:  # noqa: BLE001 - an unreadable credential is no key
        return False


def _creds(read: Reader) -> tuple[aws.Credentials, str]:
    try:
        kid, secret, reg = (read(KEY_ID) or "").strip(), (read(SECRET) or "").strip(), (read(REGION) or "").strip()
    except Exception:  # noqa: BLE001 - never says why (no values)
        raise SsmError("no key") from None
    if not kid or not secret:
        raise SsmError("no key")
    reg = reg or DEFAULT_REGION
    if not REGION_RE.fullmatch(reg):
        raise SsmError("bad region")
    return aws.Credentials(kid, secret), reg


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # no proxy from the environment


def _send(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


def _code(status: int, data: bytes) -> str:
    """AWS's error type only ("AccessDeniedException"), never its message (it names the account and the user)."""
    try:
        d = json.loads(data or b"{}")
        code = str(d.get("__type") or d.get("code") or "").split("#")[-1]
    except (ValueError, AttributeError):
        code = ""
    return code if re.fullmatch(r"[A-Za-z]{1,64}", code or "") else f"HTTP {status}"


def get(names: Iterable[str], read: Reader = credential, send: Sender | None = None, timeout: float = TIMEOUT_S,
        endpoint: str = "") -> dict[str, Param]:
    """Each NAME's parameter, PREFIX + NAME, decrypted: {name: Param}. A NAME with no parameter is left out.
    SsmError (a code, never a value) when there's no key, SSM can't be reached or it says no.
    endpoint: another base URL (tests)."""
    creds, reg = _creds(read)
    url = (endpoint or f"https://ssm.{reg}.amazonaws.com").rstrip("/") + "/"
    send = send or _send
    out: dict[str, Param] = {}
    for name in dict.fromkeys(names):
        if not PARAM_RE.fullmatch(name or ""):
            raise SsmError("bad name")
        body = json.dumps({"Name": PREFIX + name, "WithDecryption": True}, separators=(",", ":")).encode()
        hdrs = aws.sign("POST", url, {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": TARGET},
                        body, creds, reg, "ssm")
        try:
            status, data = send(url, {k: v for k, v in hdrs.items() if k != "Host"}, body, timeout)
        except Exception:  # noqa: BLE001 - a timeout, no route, a dropped connection: never its text
            raise SsmError("network") from None
        if status != 200:
            code = _code(status, data)
            if code == "ParameterNotFound":
                continue
            raise SsmError(code, status)
        try:
            p = json.loads(data)["Parameter"]
            value, version = p["Value"], int(p["Version"])
            modified = float(p.get("LastModifiedDate") or 0.0)
        except (ValueError, KeyError, TypeError):
            raise SsmError("bad answer") from None
        if not isinstance(value, str) or not value or any(c in value for c in "\n\r\0"):
            raise SsmError("bad value")
        out[name] = Param(name, value, version, modified)
    return out
