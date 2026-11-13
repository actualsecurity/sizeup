"""The station adapter's county password, set from the hub (Settings > Health): write-only.

The station's adapter reads its password from AWS SSM (sizeup/cloud/adapterssm.py, PR #417): a SecureString under a
KMS key of its own that only the station's IAM user may decrypt with. An admin may also save a new version here
instead of running the department's CLI script. The hub only ever WRITES it (ssm:PutParameter) and reads its metadata
(ssm:DescribeParameters: version, when; never the value). Its roles stay explicitly denied GetParameter*, history,
delete, label and kms:Decrypt on that prefix and key (the department's setup scripts), so the hub can never read it
back, and it never logs into the county app itself.

    configured                   the parameter's name and the key come from the environment (the task definition,
                                 from SSM by name): ENV_PARAM, ENV_KEY. Never written in the repo.
    status(store)                what Settings > Health shows: the version and when it was saved (DescribeParameters,
                                 cached), who saved it (this hub's own audit, if it was saved here), when the next
                                 save may go, and the station's adapter state (the source_health rows)
    save(new, again, confirm, by, operator, store)
                                 one PutParameter (SecureString, Overwrite, the adapter's key): the new version.
                                 Refused (SaveRefused, a fixed message) when the two differ, the value won't do, a
                                 save went less than MIN_GAP_S ago (here, or in SSM by the CLI), or the station's
                                 adapter is stopped after refused log-ons and confirm isn't given.

Each save is ONE log-on attempt on the department's shared county account (the adapter tries a new version once; 3
refused versions in 24 h stop it for a person's reset), hence the gap, the confirm and the warning on the page.

The value: wrapped in Hidden from the moment the request body is parsed (repr/str "<hidden>"), put only into the
signed request's body (a bytearray, zeroed after it is sent), never in a log, the audit, an answer or an exception's
text. AWS's own error message is never kept (a ValidationException can quote the value back): its code only. Python
can't wipe a str; nothing keeps a reference past the request.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from sizeup import aws
from sizeup.cloud.adapterssm import PREFIX, _code

ENV_PARAM = "SIZEUP_ADAPTER_PASSWORD_PARAM"     # the SecureString's full name (under PREFIX)
ENV_KEY = "SIZEUP_ADAPTER_PASSWORD_KEY"         # the adapter's KMS key: its id, ARN or alias
MIN_GAP_S = 300.0                               # saves at least this far apart (here or by the CLI): one try each
DESCRIBE_S = 60.0                               # the version is asked of SSM at most this often (page polls)
TIMEOUT_S = 10.0
MAX_BYTES = 4096                                # a Standard parameter's value
DESCRIPTION = "sizeup station adapter credential (read by the station only)"     # = the CLI's
AUDIT_SECTION, AUDIT_KEY = "system", "station adapter password"
NAME_RE = re.compile(re.escape(PREFIX) + r"[A-Z][A-Z0-9_]{0,63}")
KEY_RE = re.compile(r"(?:arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:(?:key|alias)/[A-Za-z0-9/_-]{1,256}"
                    r"|alias/[A-Za-z0-9/_-]{1,256}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                    r"|mrk-[0-9a-f]{32})")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# a source stopped after refused log-ons (the adapter's "login", "login rate", "login (manual reset needed)"): a save
# is one more attempt, so it needs the admin's explicit confirm
STOPPED_RE = re.compile(r"^login\b")
MANUAL_RE = re.compile(r"manual reset")

# send(url, headers, body, timeout) -> (HTTP status, body): urllib by default, a fake in tests
Sender = Callable[[str, dict, "bytes | bytearray", float], "tuple[int, bytes]"]


class Hidden:
    """A secret in memory only: repr/str/format never show it; not picklable, not copyable into JSON."""
    __slots__ = ("_v",)

    def __init__(self, v: str):
        self._v = v

    def reveal(self) -> str:
        return self._v

    def drop(self) -> None:
        self._v = ""

    def __repr__(self) -> str:
        return "<hidden>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<hidden>"

    def __reduce__(self):
        raise TypeError("a hidden value can't be pickled")

    def __eq__(self, other) -> bool:            # (constant-time: the two typed values)
        import hmac
        return isinstance(other, Hidden) and hmac.compare_digest(self._v.encode("utf-8", "surrogatepass"),
                                                                 other._v.encode("utf-8", "surrogatepass"))

    __hash__ = None


class SaveRefused(Exception):
    """Why a save didn't go (status: the HTTP status to answer). Its text is fixed here: never a value or AWS's text."""

    def __init__(self, msg: str, status: int = 400, wait_s: float = 0.0, code: str = "", maybe_saved: bool = False):
        super().__init__(msg)
        self.status, self.wait_s, self.code, self.maybe_saved = status, wait_s, code, maybe_saved


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # no proxy from the environment


def _send(url: str, headers: dict, body, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()


def problem(new: str) -> str:
    """Why this password won't do ("" = it will). Never echoes it."""
    if not new:
        return "Empty"
    if len(new.encode("utf-8", "surrogatepass")) > MAX_BYTES:
        return f"Too long (at most {MAX_BYTES} bytes)"
    if CONTROL.search(new):
        return "No line breaks or control characters"
    try:
        new.encode("utf-8")
    except UnicodeEncodeError:
        return "Not valid text"
    return ""


def station_sources(store) -> list[dict]:
    """The station's incident sources as last reported (source_health; stale or not): {source, label, state, reason,
    at, stopped, manual}. stopped: refused log-ons stopped it (a save is one more attempt)."""
    try:
        rows = store.q("SELECT source, label, state, reason, at, COALESCE(received, at) AS got FROM source_health "
                       "ORDER BY rank, source")
    except Exception:  # noqa: BLE001 - no table yet (no source ever reported)
        return []
    out = []
    for r in rows:
        reason = str(r["reason"] or "")
        stopped = r["state"] == "failing" and bool(STOPPED_RE.match(reason))
        out.append({"source": r["source"], "label": r["label"], "state": r["state"], "reason": reason[:200],
                    "at": r["got"], "stopped": stopped, "manual": stopped and bool(MANUAL_RE.search(reason))})
    return out


class AdapterPassword:
    def __init__(self, name: str = "", key: str = "", region: str = "", endpoint: str = "", send: Sender | None = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = print,
                 creds: Callable[[], aws.Credentials] | None = None):
        self.name, self.key = (name or "").strip(), (key or "").strip()
        self.region = aws.region(region)
        self.endpoint = endpoint
        self.send = send or _send
        self.clock, self.log = clock, log
        self.creds = creds or aws.credentials
        self._lock = threading.Lock()
        self._busy = False
        self._meta: dict | None = None       # the last DescribeParameters answer: {version, modified} | {error}
        self._meta_at = -1e18

    @classmethod
    def from_env(cls, env=None, **kw) -> "AdapterPassword":
        env = os.environ if env is None else env
        return cls(env.get(ENV_PARAM, ""), env.get(ENV_KEY, ""), **kw)

    @property
    def why_not(self) -> str:
        """"" when configured; else why not (for the page, no names)."""
        if not self.name or not self.key:
            return "Not configured"
        if not NAME_RE.fullmatch(self.name):
            return "Not configured (the parameter isn't the station adapter's)"
        if not KEY_RE.fullmatch(self.key):
            return "Not configured (not a KMS key id)"
        return ""

    @property
    def configured(self) -> bool:
        return not self.why_not

    # -- SSM ------------------------------------------------------------------------------------------------------

    def _call(self, target: str, body) -> dict:
        """One signed call; the answer's JSON. SaveRefused with AWS's error code only; maybe_saved when there was no
        answer or AWS failed on its side (a 5xx): the call may have gone through."""
        url = (self.endpoint or f"https://ssm.{self.region}.amazonaws.com").rstrip("/") + "/"
        try:
            c = self.creds()
        except aws.AwsError as e:
            raise SaveRefused("No AWS credentials", 502, code=e.code or "NoCredentials") from None
        hdrs = aws.sign("POST", url, {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": target},
                        bytes(body), c, self.region, "ssm")
        try:
            status, data = self.send(url, {k: v for k, v in hdrs.items() if k != "Host"}, body, TIMEOUT_S)
        except Exception:  # noqa: BLE001 - a timeout, no route, a dropped connection: never its text
            raise SaveRefused("No answer from AWS", 504, code="network", maybe_saved=True) from None
        if status != 200:
            if status == 403 and self.creds is aws.credentials:
                aws.forget_credentials()
            code = _code(status, data)
            raise SaveRefused(f"AWS: {code}", 502, code=code, maybe_saved=status >= 500) from None
        try:
            out = json.loads(data or b"{}")
        except ValueError:
            raise SaveRefused("AWS: an answer that isn't JSON", 502, code="BadAnswer") from None
        return out if isinstance(out, dict) else {}

    def describe(self, fresh: bool = False) -> dict:
        """{version, modified} of the parameter (metadata only), or {error}; cached DESCRIBE_S."""
        if not self.configured:
            return {"error": self.why_not}
        now = self.clock()
        with self._lock:
            if not fresh and self._meta is not None and now - self._meta_at < DESCRIBE_S:
                return dict(self._meta)
        body = json.dumps({"ParameterFilters": [{"Key": "Name", "Option": "Equals", "Values": [self.name]}]},
                          separators=(",", ":")).encode()
        try:
            got = self._call("AmazonSSM.DescribeParameters", body)
            p = next((x for x in got.get("Parameters") or [] if isinstance(x, dict) and x.get("Name") == self.name), None)
            meta = ({"version": int(p["Version"]), "modified": float(p.get("LastModifiedDate") or 0.0)} if p
                    else {"error": "Not in SSM yet"})
        except SaveRefused as e:
            meta = {"error": str(e)}
        except (KeyError, TypeError, ValueError):
            meta = {"error": "AWS: a bad answer"}
        with self._lock:
            self._meta, self._meta_at = meta, now
        return dict(meta)

    # -- the hub's own audit ----------------------------------------------------------------------------------------

    @staticmethod
    def last_saves(store, limit: int = 5) -> list[dict]:
        try:
            rows = store.q("SELECT at, operator, old, new FROM settings_audit WHERE section = ? AND key = ? "
                           "ORDER BY at DESC, id DESC LIMIT ?", (AUDIT_SECTION, AUDIT_KEY, limit))
        except Exception:  # noqa: BLE001
            return []
        return [{"at": r["at"], "by": r["operator"] or "", "result": r["new"] or ""} for r in rows]

    @staticmethod
    def _audit(store, at: float, operator: str, old: str, new: str) -> None:
        with store.tx() as db:
            db.execute("INSERT INTO settings_audit (at, operator, section, key, old, new) VALUES (?, ?, ?, ?, ?, ?)",
                       (at, operator, AUDIT_SECTION, AUDIT_KEY, old, new))

    def wait_s(self, store, meta: dict | None = None) -> float:
        """Seconds until the next save may go: MIN_GAP_S after the last attempt here or the last change in SSM."""
        last = max([s["at"] for s in self.last_saves(store, 1)] + [(meta or {}).get("modified") or 0.0, 0.0])
        return max(0.0, MIN_GAP_S - (self.clock() - last)) if last else 0.0

    # -- what the page shows -----------------------------------------------------------------------------------------

    def status(self, store) -> dict:
        meta = self.describe() if self.configured else {"error": self.why_not}
        saves = self.last_saves(store)
        version = meta.get("version")
        mine = next((s for s in saves if s["result"] == f"v{version}"), None) if version else None
        sources = station_sources(store)
        return {"configured": self.configured, "why_not": self.why_not, "version": version,
                "modified": meta.get("modified"), "error": meta.get("error", ""),
                "saved_by": mine["by"] if mine else "", "saved_here": mine is not None,
                "wait_s": round(self.wait_s(store, meta)) if self.configured else 0, "gap_s": MIN_GAP_S,
                "busy": self._busy, "saves": saves, "sources": sources,
                "stopped": any(s["stopped"] for s in sources), "manual": any(s["manual"] for s in sources)}

    # -- a save ------------------------------------------------------------------------------------------------------

    def save(self, new: Hidden, again: Hidden, confirm: bool, by: str, operator: str, store) -> dict:
        """One new version: {"version", "at"}. SaveRefused otherwise (nothing sent unless status says it may have)."""
        if not self.configured:
            raise SaveRefused(self.why_not, 404)
        if not isinstance(new, Hidden) or not isinstance(again, Hidden):
            raise SaveRefused("Bad request")
        if new != again:
            raise SaveRefused("They differ")
        if why := problem(new.reveal()):
            raise SaveRefused(why)
        with self._lock:
            if self._busy:
                raise SaveRefused("A save is going", 409)
            self._busy = True
        try:
            return self._save(new, confirm, by, operator, store)
        finally:
            with self._lock:
                self._busy = False

    def _save(self, new: Hidden, confirm: bool, by: str, operator: str, store) -> dict:
        meta = self.describe(fresh=True)
        if (w := self.wait_s(store, meta)) > 0:
            raise SaveRefused(f"Next save in {max(1, round(w / 60))} min", 429, wait_s=w)
        if any(s["stopped"] for s in station_sources(store)) and confirm is not True:
            raise SaveRefused("The adapter is stopped after refused log-ons: confirm to save", 409)
        old = f"v{meta['version']}" if meta.get("version") else ""
        who = operator or by or "someone"
        doc = {"Name": self.name, "Value": new.reveal(), "Type": "SecureString", "KeyId": self.key, "Overwrite": True,
               "Description": DESCRIPTION}
        body = bytearray(json.dumps(doc, separators=(",", ":")).encode())
        doc.clear()
        del doc
        at = self.clock()
        try:
            got = self._call("AmazonSSM.PutParameter", body)
        except SaveRefused as e:
            if e.maybe_saved:               # no answer, or AWS's own failure: it may have saved; counted as a try
                self._audit(store, at, who, old, f"no answer ({e.code or 'error'}): check the version")
                self.log(f"adapter password: save by {by or who}: {e.code or 'error'}: may have saved")
            else:
                self.log(f"adapter password: save by {by or who} refused by AWS: {e.code or 'error'}")
            raise
        finally:
            for i in range(len(body)):      # the only bytes of it we made: zeroed
                body[i] = 0
            new.drop()
        try:
            version = int(got["Version"])
        except (KeyError, TypeError, ValueError):
            self._audit(store, at, who, old, "saved (no version in the answer)")
            raise SaveRefused("AWS saved it but didn't say the version", 502) from None
        self._audit(store, at, who, old, f"v{version}")
        with self._lock:
            self._meta, self._meta_at = {"version": version, "modified": at}, at
        self.log(f"adapter password: v{version} saved by {by or who}")
        return {"version": version, "at": at}
