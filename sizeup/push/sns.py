"""Amazon SNS mobile push over the existing stdlib SigV4 (sizeup/aws.py `sign`: the outbound guard refuses a
hand-started or local hub before anything leaves). The Query API (form POST, XML answers), no boto3.

    Sns(region, endpoint="")                          endpoint: another base URL (tests: a fake on this machine)
    sns.create_endpoint(app_arn, token, user_data)    -> the endpoint's ARN (the same one for the same token)
    sns.set_attributes(endpoint_arn, {...})           Token / Enabled
    sns.delete_endpoint(endpoint_arn)
    sns.publish(endpoint_arn, message, attributes)    message: {"APNS": "<json>"} etc. (MessageStructure=json)

Apps.from_env(): the three platform applications, from OPTIONAL environment variables (ARNs are not secrets, but they
are not in this repository; the hub's task definition is not changed for them, see docs/mobile-api.md section 4):

    SIZEUP_SNS_APNS_SANDBOX_ARN   arn:aws:sns:<region>:<account>:app/APNS_SANDBOX/<name>   debug builds
    SIZEUP_SNS_APNS_ARN           arn:aws:sns:<region>:<account>:app/APNS/<name>           TestFlight / store builds
    SIZEUP_SNS_FCM_ARN            arn:aws:sns:<region>:<account>:app/GCM/<name>            Android (FCM)
    SIZEUP_PUSH_CRITICAL          1 = the Critical Alerts entitlement is granted: dispatch / emergency pushes use it
    SIZEUP_SNS_ENDPOINT           a loopback URL only (a local fake for a demo); never an outside host

A variable that is empty, missing or not an ARN of the right platform leaves that platform off: a registration for it is
refused, so a sandbox token can never go to the production application (and the reverse)."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from typing import Callable

from sizeup import aws

VERSION = "2010-03-31"
ARN = re.compile(r"^arn:aws:sns:(?P<region>[a-z0-9-]{4,20}):\d{12}:app/(?P<platform>APNS_SANDBOX|APNS|GCM)/"
                 r"[\w.-]{1,256}$")
LOCAL = ("127.0.0.1", "::1", "localhost")


class SnsError(aws.AwsError):
    """SNS refused or couldn't be reached. code: SNS's own ("EndpointDisabled", "NotFound") or ours ("OutboundOff",
    "Network")."""


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _xml(raw: bytes) -> ET.Element:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise SnsError("sns: an answer that isn't XML", "BadAnswer") from None
    for el in root.iter():
        el.tag = _local(el.tag)
    return root


class Sns:
    def __init__(self, region: str, endpoint: str = "", creds: Callable[[], aws.Credentials] = aws.credentials,
                 timeout: float = 8.0):
        """creds: the credentials to sign with, fetched per call (the task role's, cached in sizeup/aws.py): the same
        seam as sizeup/attachments.py and sizeup/audiostore.py."""
        self.region, self.creds, self.timeout = aws.region(region), creds, timeout
        self.endpoint = (endpoint or f"https://sns.{self.region}.amazonaws.com").rstrip("/") + "/"

    def _call(self, action: str, params: dict) -> ET.Element:
        form = {"Action": action, "Version": VERSION, **params}
        data = urllib.parse.urlencode(sorted(form.items())).encode()
        try:
            c = self.creds()
            hdrs = aws.sign("POST", self.endpoint, {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
                            data, c, self.region, "sns")
        except aws.AwsError as e:                       # OutboundOff, NoCredentials: nothing was sent
            raise SnsError(str(e), e.code, e.status) from None
        req = urllib.request.Request(self.endpoint, data=data, method="POST",
                                     headers={k: v for k, v in hdrs.items() if k != "Host"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read(1 << 20)
        except urllib.error.HTTPError as e:
            if e.code == 403 and self.creds is aws.credentials:
                aws.forget_credentials()
            code, msg = "", ""
            try:
                err = _xml(e.read(8192)).find(".//Error")
                code, msg = (err.findtext("Code") or ""), (err.findtext("Message") or "")
            except (SnsError, AttributeError, OSError):
                pass
            raise SnsError(f"{code or 'HTTP ' + str(e.code)}: {msg}"[:300], code or f"HTTP {e.code}", e.code) from None
        except OSError as e:
            raise SnsError(f"sns: {getattr(e, 'reason', e)}", "Network") from None
        return _xml(raw)

    def create_endpoint(self, app_arn: str, token: str, user_data: str = "") -> str:
        p = {"PlatformApplicationArn": app_arn, "Token": token}
        if user_data:
            p["CustomUserData"] = user_data[:2048]
        arn = self._call("CreatePlatformEndpoint", p).findtext(".//EndpointArn") or ""
        if not arn:
            raise SnsError("sns: no endpoint in the answer", "BadAnswer")
        return arn

    def set_attributes(self, endpoint_arn: str, attrs: dict[str, str]) -> None:
        p = {"EndpointArn": endpoint_arn}
        for i, (k, v) in enumerate(sorted(attrs.items()), 1):
            p[f"Attributes.entry.{i}.key"], p[f"Attributes.entry.{i}.value"] = k, v
        self._call("SetEndpointAttributes", p)

    def delete_endpoint(self, endpoint_arn: str) -> None:
        self._call("DeleteEndpoint", {"EndpointArn": endpoint_arn})

    def publish(self, endpoint_arn: str, message: dict[str, str], attributes: dict[str, str] | None = None) -> str:
        """One Publish to one endpoint. message: platform key -> its JSON string (MessageStructure=json needs a
        "default" too: the title). attributes: SNS message attributes (String), e.g. AWS.SNS.MOBILE.APNS.PRIORITY."""
        p = {"TargetArn": endpoint_arn, "MessageStructure": "json",
             "Message": json.dumps(message, separators=(",", ":"))}
        for i, (k, v) in enumerate(sorted((attributes or {}).items()), 1):
            p[f"MessageAttributes.entry.{i}.Name"] = k
            p[f"MessageAttributes.entry.{i}.Value.DataType"] = "String"
            p[f"MessageAttributes.entry.{i}.Value.StringValue"] = str(v)
        return self._call("Publish", p).findtext(".//MessageId") or ""


@dataclass(frozen=True)
class Apps:
    """The platform applications this hub knows. app(platform, environment) -> its ARN, or "" (push off for it)."""
    apns_sandbox: str = ""
    apns: str = ""
    fcm: str = ""
    critical: bool = False        # the Critical Alerts entitlement is granted (SIZEUP_PUSH_CRITICAL=1)
    endpoint: str = ""

    @classmethod
    def from_env(cls, env=None) -> "Apps":
        env = os.environ if env is None else env

        def arn(name: str, platform: str) -> str:
            v = str(env.get(name, "")).strip()
            m = ARN.match(v)
            return v if m and m.group("platform") == platform else ""     # a wrong-platform ARN leaves it off
        endpoint = str(env.get("SIZEUP_SNS_ENDPOINT", "")).strip()
        if endpoint and (urllib.parse.urlsplit(endpoint).hostname or "") not in LOCAL:
            endpoint = ""
        return cls(arn("SIZEUP_SNS_APNS_SANDBOX_ARN", "APNS_SANDBOX"), arn("SIZEUP_SNS_APNS_ARN", "APNS"),
                   arn("SIZEUP_SNS_FCM_ARN", "GCM"), str(env.get("SIZEUP_PUSH_CRITICAL", "")).strip() == "1", endpoint)

    def app(self, platform: str, environment: str = "") -> str:
        if platform == "ios":
            return {"sandbox": self.apns_sandbox, "production": self.apns}.get(environment, "")
        return self.fcm if platform == "android" else ""

    def configured(self) -> bool:
        return bool(self.apns_sandbox or self.apns or self.fcm)

    def region(self) -> str:
        for a in (self.apns, self.apns_sandbox, self.fcm):
            if a:
                return a.split(":")[3]
        return ""


def endpoint_platform(endpoint_arn: str) -> str:
    """APNS_SANDBOX | APNS | GCM from an endpoint ARN (arn:aws:sns:r:a:endpoint/APNS/<app>/<id>), else ""."""
    parts = endpoint_arn.split(":", 5)[-1].split("/")
    ok = len(parts) >= 3 and parts[0] == "endpoint" and parts[1] in ("APNS_SANDBOX", "APNS", "GCM")
    return parts[1] if ok else ""
