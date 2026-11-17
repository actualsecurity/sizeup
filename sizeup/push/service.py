"""PushService (#563): the registered devices, and the fan-out of a kind's message to them.

Registration (the app's PUT / DELETE /api/v1/devices/{id}/push, sizeup/api/v1/push.py): register(), unregister(), and
drop() / `drop_device`, which the sign-in's Revoke is meant to call: NOT CALLED YET, wired when Revoke lands (#558).
Fan-out: `offer()`, called by `notify.Pushes.fan_out` beside the ntfy path (before ntfy's own "is there a topic"
check on purpose: devices get a kind's pushes even when it has no ntfy topic), never blocks and never raises; one
worker thread (started on the first message, gone after 30 s idle, like the Notifier's) does the work: one Publish
per endpoint, at most MAX_DEVICES, within one deadline per message.

Who gets a kind (docs/mobile-api.md section 4): alerts, intel and dispatch: every signed-in, active person's device that
subscribed to it; status: admins. A device that never subscribed to a kind does not get it (dispatch is opt-in). The
person's role is looked up when the push goes out (`role_of(email)`: admin | dispatcher | viewer | None for an
unknown or deactivated person, who gets nothing). `device_active(device_id)` is asked for EVERY device on every send
(default: all devices are active): #558 wires it to its devices table, so a revoked device of an active person gets
nothing even if its endpoint row is still here. Quiet hours (the device's own, in its zone) hold intel and status,
never alerts or dispatch.

Policy per kind, the intel `policy` pattern (the settings' section "notify", key "devices": {"off": true,
"<kind>": {"on", "per_minute", "per_hour", "per_day"}}, set by an admin: sizeup/cad/settings.py, checked by
validate_policy), read at every message: `on: false` is the kill switch, and dispatch is off until it is turned on (it
needs the department's sign-off and Apple's Critical Alerts entitlement, D3). Over a cap the message is not sent to the
phones (ntfy is unaffected); a loop cannot page 40 phones a second.

Fail closed: no platform application configured, the master switch or the `push` feature off, outbound off (the guard in
aws.sign: the first Publish raises OutboundOff and the rest are skipped), this task not the leader (during a handover
overlap only the leader publishes, so phones never get a page twice), a stale endpoint (SNS says disabled or gone) is
removed with its row."""
from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sizeup.fence import FencedWrite
from sizeup.push import content, sns as snsmod
from sizeup.push.devices import MAX_DEVICES, Full, PushDevices

PLATFORMS = ("ios", "android")
ENVIRONMENTS = ("sandbox", "production")
TOKEN = re.compile(r"[A-Za-z0-9:_\-.=]{16,1024}")
HM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
CAPS = ("per_minute", "per_hour", "per_day")
DEFAULT_PREFS = {"alerts": True, "intel": True, "status": True, "dispatch": False}
DEFAULT_POLICY = {
    "alerts": {"on": True, "per_minute": 10, "per_hour": 60, "per_day": 300},
    "intel": {"on": True, "per_minute": 6, "per_hour": 30, "per_day": 200},
    "status": {"on": True, "per_minute": 4, "per_hour": 20, "per_day": 100},
    "dispatch": {"on": False, "per_minute": 10, "per_hour": 120, "per_day": 600},      # off until switched on (D3)
}
MINUTE_S, HOUR_S, DAY_S = 60, 3600, 86400
MAX_CAP = 100000                  # the largest per_minute / per_hour / per_day an admin may set
QUEUE_MAX = 50                    # messages waiting for the worker: at 40 devices that is already 2000 calls
IDLE_S = 30.0
SAY_EVERY_S = 60.0                # a repeated problem is logged at most this often
DEADLINE_S = 30.0                 # one message's whole fan-out (40 serial Publishes at 8 s each could be 5 minutes)
STALE = frozenset({"EndpointDisabled", "NotFound"})      # not InvalidParameter: that could be our payload, not a token


class PushError(Exception):
    """A registration that can't be done: status, machine code, message (the app's error envelope)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def policy(ov: dict | None) -> dict:
    """The settings' devices section over the defaults; bad values are the default (a broken section never turns a
    kind on). {"off": true} is every kind off."""
    out = {k: dict(v) for k, v in DEFAULT_POLICY.items()}
    ov = ov if isinstance(ov, dict) else {}
    for kind, cur in out.items():
        mine = ov.get(kind)
        if not isinstance(mine, dict):
            continue
        if isinstance(mine.get("on"), bool):
            cur["on"] = mine["on"]
        for k in CAPS:
            v = mine.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_CAP:
                cur[k] = v
    if ov.get("off") is True:
        for cur in out.values():
            cur["on"] = False
    return out


def validate_policy(d) -> dict:
    """What Settings may store under notify > devices: only the kinds, and `on` / the caps with their ranges, or `off`.
    ValueError(message) for anything else (the admin's form is told; policy() above is the lenient reader)."""
    if not isinstance(d, dict) or set(d) - set(DEFAULT_POLICY) - {"off"}:
        raise ValueError("devices: off, or alerts / intel / status / dispatch")
    out: dict = {}
    if "off" in d:
        if not isinstance(d["off"], bool):
            raise ValueError("devices.off: true or false")
        out["off"] = d["off"]
    for kind in DEFAULT_POLICY:
        if kind not in d:
            continue
        mine = d[kind]
        if not isinstance(mine, dict) or set(mine) - {"on", *CAPS}:
            raise ValueError(f"devices.{kind}: on, per_minute, per_hour, per_day")
        out[kind] = {}
        for k, v in mine.items():
            if k == "on" and not isinstance(v, bool):
                raise ValueError(f"devices.{kind}.on: true or false")
            if k in CAPS and (not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= MAX_CAP):
                raise ValueError(f"devices.{kind}.{k}: a whole number, 1 to {MAX_CAP}")
            out[kind][k] = v
    return out


def in_quiet(quiet: dict | None, now: float) -> bool:
    """Is `now` inside the device's quiet hours? The window is [from, to) on the device's own clock in its zone and may
    cross midnight (22:00 to 06:00); from == to is no window; a broken record is never quiet (a page is not held on a
    guess). "00:00" to "24:00" is not accepted (the pattern stops at 23:59): use two registrations or no quiet hours."""
    if not quiet:
        return False
    try:
        a, b = HM.fullmatch(quiet["from"]), HM.fullmatch(quiet["to"])
        t = datetime.fromtimestamp(now, ZoneInfo(quiet["tz"]))
        lo, hi = int(a[1]) * 60 + int(a[2]), int(b[1]) * 60 + int(b[2])
    except (KeyError, TypeError, ZoneInfoNotFoundError, ValueError):
        return False
    m = t.hour * 60 + t.minute
    if lo == hi:
        return False
    return lo <= m < hi if lo < hi else (m >= lo or m < hi)


class PushService:
    def __init__(self, devices: PushDevices, apps: snsmod.Apps, sns: snsmod.Sns | None = None,
                 settings: Callable[[], dict] | None = None, active: Callable[[], bool] | None = None,
                 role_of: Callable[[str], str | None] | None = None,
                 device_active: Callable[[str], bool] | None = None, clock=time.time, log=print,
                 mono: Callable[[], float] = time.monotonic, unit_of: Callable[[str], str | None] | None = None):
        self.devices, self.apps, self.sns = devices, apps, sns
        self.settings = settings or (lambda: {})
        self.active = active or (lambda: True)
        self.role_of = role_of or (lambda email: None)       # no way to tell a role: nobody gets anything
        # #558 wires its devices table here (a revoked device: False)
        self.device_active = device_active or (lambda device_id: True)
        self.device_active_wired = device_active is not None      # not wired: registering warns once (register())
        # a unit code -> the CAD list's own name, None when it is not a unit; none given: no unit is named on a phone
        self.unit_of = unit_of or (lambda code: None)
        self.clock, self.log, self.mono = clock, log, mono
        self._sent: dict[str, deque] = {k: deque() for k in content.KINDS}
        self._q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.stats = {"sent": 0, "failed": 0, "capped": 0, "killed": 0, "removed": 0, "held": 0, "skipped": 0}
        self._said: dict[str, float] = {}

    def _client(self) -> snsmod.Sns:
        if self.sns is None:
            self.sns = snsmod.Sns(self.apps.region(), self.apps.endpoint)
        return self.sns

    def _say(self, key: str, msg: str) -> None:
        now = self.clock()
        if now - self._said.get(key, -1e9) >= SAY_EVERY_S:
            self._said[key] = now
            try:
                self.log(msg)
            except Exception:  # noqa: BLE001
                pass

    # -- registration -----------------------------------------------------------------------------------------------

    def register(self, device_id: str, email: str, platform: str, environment: str, token: str, prefs: dict | None,
                 quiet: dict | None) -> dict:
        """Register or replace a device's endpoint: the stored row. PushError: push isn't set up for that platform
        (503), SNS refused (502), full (409). FencedWrite (the 503 handover) propagates."""
        app = self.apps.app(platform, environment)
        if not app:
            raise PushError(503, "push_unavailable", "push is not set up for this platform on this hub")
        if not self.devices.writable():              # not the leader: refuse before SNS makes an endpoint nobody keeps
            raise FencedWrite("push: not the leader")
        if not self.device_active_wired:
            self._say("device-active", "push: device_active not wired: revoked devices keep receiving pushes")
        old = self.devices.get(device_id)
        if old is None and self.devices.count() >= MAX_DEVICES:
            raise PushError(409, "push_full", "too many devices are registered for push")
        sns = self._client()
        try:
            if old and old["app"] != app:            # a build that moved between the sandbox and production
                self._delete(old["endpoint"])
                old = None
            arn = sns.create_endpoint(app, token, device_id)
            sns.set_attributes(arn, {"Token": token, "Enabled": "true"})
            if old and old["endpoint"] != arn:
                self._delete(old["endpoint"])
        except snsmod.SnsError as e:
            self.log(f"push: register failed: {e.code}")
            if e.code == "OutboundOff":              # a hand-started hub: it makes no endpoint at SNS either
                raise PushError(503, "push_unavailable", "push is not set up on this hub") from None
            raise PushError(502, "push_failed", "the push service refused it: try again") from None
        merged = {**DEFAULT_PREFS, **(prefs or {})}
        try:
            return self.devices.put(device_id, email, platform, "" if platform == "android" else environment, arn, app,
                                    merged, quiet)
        except Full:            # the count above and this insert are two steps: a race for the last place ends here
            self._delete(arn)
            raise PushError(409, "push_full", "too many devices are registered for push") from None

    def _delete(self, endpoint: str) -> None:
        try:
            self._client().delete_endpoint(endpoint)
        except snsmod.SnsError as e:                  # an orphan costs nothing: no row, so nothing ever publishes to it
            self.log(f"push: delete endpoint: {e.code}")

    def unregister(self, device_id: str) -> bool:
        """Remove the device's endpoint and row (the row goes even if SNS can't delete the endpoint now). Never
        raises but FencedWrite."""
        row = self.devices.remove(device_id)
        if row:
            self._delete(row["endpoint"])
        return row is not None

    drop = unregister       # the sign-in's Revoke will call `drop`: the same act, named for what a revoke does

    # -- fan-out ----------------------------------------------------------------------------------------------------

    def offer(self, kind: str, title: str, body: str, priority=3, who=None, click: str = "", ref: str = "") -> None:
        """Queue one message for the app's devices. Never raises, never waits. Nothing is queued while push is off or
        this task is not the leader (the old and the new task of a handover must not both page the phones)."""
        try:
            if (kind not in content.KINDS or not self.apps.configured() or not self.active()
                    or not self.devices.writable()):
                return
            self._q.put_nowait((kind, title, body, priority, who, click, ref))
        except queue.Full:
            self._say("full", "push: too many waiting: device pushes dropped")
            return
        except Exception as e:  # noqa: BLE001 - the ntfy path is never held up by this one
            self._say("offer", f"push: offer failed: {type(e).__name__}")
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="push-devices", daemon=True)
                self._thread.start()

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything queued was delivered or given up on. For tests only."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=IDLE_S)
            except queue.Empty:
                return
            try:
                self.deliver(*item)
            except Exception as e:  # noqa: BLE001
                self._say("deliver", f"push: failed: {type(e).__name__}")
            finally:
                self._q.task_done()

    def _allowed(self, kind: str) -> bool:
        """The kill switch and the caps. A message that passes is counted."""
        try:
            pol = policy((self.settings() or {}))[kind]
        except Exception:  # noqa: BLE001 - settings unreadable: the defaults
            pol = DEFAULT_POLICY[kind]
        if not pol["on"]:
            self.stats["killed"] += 1
            return False
        now, sent = self.clock(), self._sent[kind]
        while sent and sent[0] <= now - DAY_S:
            sent.popleft()
        n_min = sum(1 for t in sent if t > now - MINUTE_S)
        n_hour = sum(1 for t in sent if t > now - HOUR_S)
        if n_min >= pol["per_minute"] or n_hour >= pol["per_hour"] or len(sent) >= pol["per_day"]:
            self.stats["capped"] += 1
            self._say(f"cap-{kind}", f"push: {kind}: over its cap: not sent to devices")
            return False
        sent.append(now)
        return True

    def _audience(self, kind: str, now: float) -> list[dict]:
        """The devices that get this kind now: subscribed, of an active person whose role allows it (status: admins),
        not revoked, not in their quiet hours (intel and status), on the platform their endpoint was made for."""
        out = []
        for r in self.devices.rows():
            if r["prefs"].get(kind, DEFAULT_PREFS[kind]) is not True:
                continue
            role = self.role_of(r["email"])
            if role is None or (kind == "status" and role != "admin") or not self.device_active(r["device_id"]):
                continue
            if kind not in content.CRITICAL_KINDS and in_quiet(r["quiet"], now):
                self.stats["held"] += 1
                continue
            if snsmod.endpoint_platform(r["endpoint"]) != content.platform_key(r["platform"], r["env"]):
                self._say("mismatch", "push: an endpoint of another platform than its device's: skipped")
                continue
            out.append(r)
        return out

    def _publish_one(self, r: dict, kind: str, mid: str, title: str, body: str, lvl: str) -> str:
        """One Publish: "sent", "stop" (the outbound guard: nothing leaves this process), or "failed"."""
        gcm = r["platform"] == "android"
        payload = (content.fcm if gcm else content.apns)(kind, mid, title, body, lvl)
        try:
            self._client().publish(r["endpoint"], content.message(r["platform"], r["env"], payload),
                                   {} if gcm else content.apns_attributes(lvl))
        except snsmod.SnsError as e:
            if e.code == "OutboundOff":
                self._say("outbound", f"push: {e}")
                return "stop"
            self.stats["failed"] += 1
            if e.code in STALE:                         # the phone's token is gone: forget it (the app registers again)
                self.stats["removed"] += 1
                try:
                    self.unregister(r["device_id"])
                except Exception as x:  # noqa: BLE001 - a fenced write: the leader will
                    self._say("stale", f"push: a stale endpoint kept ({type(x).__name__})")
            else:
                self._say(f"err-{e.code}", f"push: publish failed: {e.code}")
            return "failed"
        self.stats["sent"] += 1
        return "sent"

    def deliver(self, kind: str, title: str, body: str, priority=3, who=None, click: str = "", ref: str = "") -> int:
        """Publish one message to every device that may have it: the number sent. (The worker's job; tests call it.)"""
        if not self.devices.writable():          # not the leader (a handover overlap): the leader publishes
            self.stats["skipped"] += 1
            return 0
        if not self._allowed(kind):
            return 0
        rows = self._audience(kind, self.clock())
        if not rows:
            return 0
        t, b, why = content.device_text(kind, title, body, who, self.unit_of)
        if why:
            self._say(f"scrub-{kind}", f"push: {kind}: text held back ({why}): generic push")
        mid = content.message_id(kind, click, who, ref)
        lvl = content.level(kind, priority, self.apps.critical)
        end, sent = self.mono() + DEADLINE_S, 0
        for r in rows:
            if self.mono() > end:
                self._say("deadline", "push: a fan-out ran out of time: the rest were not sent")
                break
            result = self._publish_one(r, kind, mid, t, b, lvl)
            if result == "stop":
                break
            sent += result == "sent"
        return sent


# The sign-in's Revoke (#558, built apart) is meant to call drop_device: NOT CALLED YET, wired when Revoke lands. The
# service is module-level because the routes (api/v1/push.py) and Revoke are plain functions with no hub object to hold
# it (a V1 / Ctx field would be the alternative); the hub's start installs it once (wire.py), tests install a fake.
_service: PushService | None = None


def install(service: PushService | None) -> None:
    global _service
    _service = service


def installed() -> PushService | None:
    return _service


def drop_device(device_id: str) -> bool:
    """Remove a device's push endpoint and row (Revoke, and a person's removal: not called yet, #558). True when there
    was one. Never raises (but FencedWrite, the handover signal): an SNS failure never holds a revoke up, an orphan
    endpoint is harmless (the row is gone, so nothing publishes to it)."""
    svc = _service
    if svc is None:
        return False
    try:
        return svc.drop(device_id)
    except FencedWrite:
        raise                              # not the leader: the revoke must say so (the 503 handover), not pretend
    except Exception as e:  # noqa: BLE001
        svc.log(f"push: drop_device: {type(e).__name__}")
        return False
