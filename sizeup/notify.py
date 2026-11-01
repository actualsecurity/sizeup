"""Push notifications out through ntfy (https://ntfy.sh): the one place sizeup publishes anything.

    publish(server, topic, title, body, priority=3, tags=(), click="", who=None)   one message, now;
                                     NotifyError with the reason when ntfy didn't take it
    Notifier(server, topic, log)     the same from a background thread (send() never blocks the caller,
                                     a few retries on a network error), for the hub's station-health alerts
                                     (sizeup/cloud/health.py) and anything else that pages a person (#128)

Who (user, 2026-10-14: "we want it to include info on who was called or mentioned"): every message
can carry a Who (or a dict of the same fields): the units (code and name), the talker, the channel, the
incident (number, address), the station, the dongle, and a short detail. who_line() renders it the same
way for every caller, after the title, so a phone's lock screen says who and where:

    MAYDAY · E64 P1 (Engine 64) on FG1 · 26-000015 100 S Ridge St
    Station silent 4 min · Port Chester FD · last heard 14:32
    FG1 not running · SDR 00000102 on FG1

Publishing is ntfy's JSON form (POST <server>/ with {topic, title, message, priority, tags, click}), so
titles and bodies can be any UTF-8. ntfy topics are public by name: whoever knows the name can read and
post, so a topic of ours is a long random name (docs/cloud.md "Station health and alerts").

Two topics, by kind (route(), notifiers(); user, 2026-11-01):

    ALERTS  [alerts] ntfy_topic / SIZEUP_ALERT_TOPIC (the hub: SSM /sizeup-hub/alert-topic): incident-level
            alarms: the emergency buttons (P25, MDC, FleetSync) and the fireground mayday (sizeup/cad/alarms.py),
            aircraft emergency squawks (sizeup/aircraft.py through the alarms), their acknowledgements, all
            clears and "Ended" notices
    STATUS  [alerts] status_topic / SIZEUP_STATUS_TOPIC (the hub: SSM /sizeup-hub/status-topic): the system's
            own health: station health (sizeup/cloud/health.py: silent, behind, update failed, a dongle gone, a
            job / whisper / trunk-recorder down, a stuck channel, the outbox, the disk, the whisper queue), Amazon
            Transcribe failing, its monthly budget at 80 % / 100 %, and their recovery notices

Only one of them set: every kind goes there (as before the split). Neither: no pushes. The app logs at startup
where each kind goes (route_line(): "set" or "off", never a topic's name).

NEVER Tapout's topics: pcfd-phone-prealert pages the fire crew's phones, pcfd-status is Tapout's own
health feed. sizeup only reads those (sizeup/prealert.py). topic_problem() refuses them (and anything that
isn't a plain topic name), the config refuses them ([alerts] ntfy_topic), and publish() refuses them again
right before a request is made: a mistake in one place can't page the crew. The match is the whole name
(case aside): a topic of ours that merely contains "status" or "prealert" is fine.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_SERVER = "https://ntfy.sh"
# Tapout's topics (sizeup/prealert.py, firehouse-ring-alerter): never published to, whatever the config says.
FORBIDDEN = frozenset({"pcfd-phone-prealert", "pcfd-status"})
TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")
MIN_TOPIC_LEN = 12          # a short name is easy to guess (and to read along with)
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5, "max": 5}
ALERTS, STATUS = "alerts", "status"     # the kinds of push (route()): incident alarms; the system's own health
KINDS = (ALERTS, STATUS)
TIMEOUT_S = 10.0
RETRIES = (2.0, 10.0, 30.0)     # Notifier: waits between attempts after a network error or a 5xx/429


class NotifyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Who:
    """Who and where a notification is about; every field optional."""
    units: tuple = ()          # ("E64 P1", "Engine 64") pairs (code, name), or plain codes
    talker: str = ""           # who keyed up, when not one of the units (a radio ID, an alias)
    channel: str = ""          # "FG1", "Fire/EMS 15"
    incident: str = ""         # the CAD's event number "26-000015"
    address: str = ""          # "100 S Ridge St"
    station: str = ""          # the station's name (station health)
    dongle: str = ""           # "SDR 00000102"
    detail: str = ""           # "last heard 14:32"

    @classmethod
    def of(cls, x) -> Who | None:
        """A Who from a Who, a dict with these keys (others ignored) or None."""
        if x is None or isinstance(x, Who):
            return x
        if not isinstance(x, dict):
            raise TypeError("who: a Who or a dict")
        names = {f.name for f in fields(cls)}
        d = {k: v for k, v in x.items() if k in names}
        units = d.get("units") or ()
        d["units"] = tuple(tuple(u)[:2] if isinstance(u, (list, tuple)) else (str(u),) for u in units)
        return cls(**{k: (v if k == "units" else str(v or "")) for k, v in d.items()})

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v}


def _unit(u) -> str:
    code, name = (tuple(u) + ("", ""))[:2] if isinstance(u, (list, tuple)) else (str(u), "")
    code, name = str(code).strip(), str(name).strip()
    return f"{code} ({name})" if name and code and name.lower() != code.lower() else (code or name)


def who_line(who) -> str:
    """The Who as one line: units (or the talker) on the channel · the incident and address · the station ·
    the dongle · the detail. "" for nobody."""
    w = Who.of(who)
    if w is None:
        return ""
    head = ", ".join(x for x in (_unit(u) for u in w.units) if x) or w.talker.strip()
    if w.units and w.talker.strip() and w.talker.strip() not in head:
        head += f" ({w.talker.strip()})"
    dongle = w.dongle.strip()
    if w.channel.strip():
        on = f"on {w.channel.strip()}"
        if head:
            head = f"{head} {on}"
        elif dongle:
            dongle, head = f"{dongle} {on}", ""
        else:
            head = w.channel.strip()
    inc = " ".join(x for x in (w.incident.strip(), w.address.strip()) if x)
    return " · ".join(x for x in (head, inc, w.station.strip(), dongle, w.detail.strip()) if x)


def compose(title: str, who=None) -> str:
    """The push title: the title, then who_line() ("MAYDAY · E64 P1 (Engine 64) on FG1 · 26-000015 ...")."""
    line = who_line(who)
    return f"{title} · {line}" if line and title else (title or line)


def topic_problem(topic: str, also_forbidden: Iterable[str] = ()) -> str:
    """"" for a topic sizeup may publish to, else why not."""
    t = (topic or "").strip()
    if not TOPIC.fullmatch(t):
        return "an ntfy topic name: letters, digits, - and _ (at most 64)"
    banned = {x.lower() for x in FORBIDDEN} | {str(x).strip().lower() for x in also_forbidden if str(x).strip()}
    if t.lower() in banned:
        return (f"{t!r} is Tapout's topic (it pages the fire crew / reports Tapout's health): never publish there; "
                "choose a new, long random topic of our own")
    if len(t) < MIN_TOPIC_LEN:
        return f"at least {MIN_TOPIC_LEN} characters: ntfy topics are public by name, so make it long and random"
    return ""


def route(alert_topic: str = "", status_topic: str = "") -> dict[str, str]:
    """Which topic each kind of push goes to: {ALERTS: topic, STATUS: topic}, "" for none. Only one topic set:
    both kinds go to it. Neither: {ALERTS: "", STATUS: ""} (no pushes)."""
    a, s = (alert_topic or "").strip(), (status_topic or "").strip()
    return {ALERTS: a or s, STATUS: s or a}


def route_line(alert_topic: str = "", status_topic: str = "") -> str:
    """The startup line, by kind and never with a topic's name: "notify: alerts \u2192 topic set, status \u2192
    topic set"; a kind without a topic of its own says it shares the other's; neither: no pushes."""
    a, s = bool((alert_topic or "").strip()), bool((status_topic or "").strip())
    if not a and not s:
        return "notify: no pushes (no [alerts] ntfy_topic / status_topic set)"
    alerts = "topic set" if a else "the status topic (no alerts topic set)"
    status = "topic set" if s else "the alerts topic (no status topic set)"
    return f"notify: alerts \u2192 {alerts}, status \u2192 {status}"


def notifiers(server: str, alert_topic: str = "", status_topic: str = "", log: Callable[[str], None] = print,
              **kw) -> dict[str, Notifier | None]:
    """{ALERTS: Notifier | None, STATUS: Notifier | None} by route(): one Notifier (one queue, in order) when
    both kinds go to the same topic, None for a kind with nowhere to go. kw: Notifier's (sleep, retries)."""
    r = route(alert_topic, status_topic)
    made: dict[str, Notifier] = {}
    out: dict[str, Notifier | None] = {}
    for kind in KINDS:
        t = r[kind]
        if t and t not in made:
            made[t] = Notifier(server, t, log, **kw)
        out[kind] = made.get(t) if t else None
    return out


def server_problem(server: str) -> str:
    """"" for a usable ntfy server URL (https, or plain http only to this machine: a test server)."""
    m = re.fullmatch(r"(https?)://([^/:?#\s]+)(:\d+)?/?", server or "")
    if not m:
        return 'the ntfy server, "https://ntfy.sh"'
    if m.group(1) == "http" and m.group(2) not in ("127.0.0.1", "localhost", "::1"):
        return "https:// (plain http only to this machine)"
    return ""


def _priority(p) -> int:
    if isinstance(p, str):
        if p.strip().lower() in PRIORITIES:
            return PRIORITIES[p.strip().lower()]
        p = int(p) if p.strip().isdigit() else 3
    return min(5, max(1, int(p or 3)))


def publish(server: str, topic: str, title: str, body: str, priority: int | str = 3,
            tags: Iterable[str] = (), click: str = "", timeout: float = TIMEOUT_S, who=None) -> dict:
    """One message to ntfy: its answer ({id, time, ...}). NotifyError when it can't be sent or was refused.
    who: a Who / dict: rendered into the title (compose())."""
    server = (server or DEFAULT_SERVER).rstrip("/")
    why = server_problem(server) or topic_problem(topic)
    if why:
        raise NotifyError(why)
    title = compose(str(title), who)
    msg = {"topic": topic.strip(), "title": title[:250], "message": str(body)[:4000] or title[:250],
           "priority": _priority(priority), "tags": [str(t)[:40] for t in tags if str(t).strip()][:5]}
    if click and urlsplit(click).scheme in ("https", "http"):
        msg["click"] = click
    req = Request(server + "/", data=json.dumps(msg).encode(), method="POST",
                  headers={"Content-Type": "application/json", "User-Agent": "sizeup-notify"})
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read(65536)
    except HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read(4096) or b"{}").get("error", "")
        except (ValueError, AttributeError, OSError):
            pass
        err = NotifyError(f"ntfy refused it: HTTP {e.code}" + (f" ({detail})" if detail else ""))
        err.status = e.code  # type: ignore[attr-defined]
        raise err from None
    except (URLError, OSError, TimeoutError) as e:
        raise NotifyError(f"can't reach {urlsplit(server).hostname}: {getattr(e, 'reason', e)}") from None
    try:
        out = json.loads(raw or b"{}")
    except ValueError:
        out = {}
    return out if isinstance(out, dict) else {}


class Notifier:
    """publish() from one background thread, in order: send() returns at once. Off (send() does nothing,
    enabled False) without a topic. A message that can't go after a few tries is logged and dropped."""

    def __init__(self, server: str, topic: str, log: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep, retries: tuple[float, ...] = RETRIES):
        self.server, self.topic, self.log = (server or DEFAULT_SERVER).rstrip("/"), (topic or "").strip(), log
        self.sleep, self.retries = sleep, retries
        why = (server_problem(self.server) or topic_problem(self.topic)) if self.topic else ""
        if why:
            log(f"notify: off: {why}")
            self.topic = ""
        self.sent = self.failed = 0
        self.last_error = ""
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    def send(self, title: str, body: str, priority: int | str = 3, tags: Iterable[str] = (), click: str = "",
             who=None) -> bool:
        """Queued for sending: True; off or the queue full: False. who: a Who / dict (compose())."""
        if not self.enabled:
            return False
        try:
            self._q.put_nowait((title, body, priority, tuple(tags), click, Who.of(who)))
        except queue.Full:
            self.log("notify: too many waiting: dropped " + repr(title[:80]))
            return False
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="notify", daemon=True)
                self._thread.start()
        return True

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until everything queued was sent or given up on (tests)."""
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.02)
        return False

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=30.0)
            except queue.Empty:
                return
            try:
                self._deliver(*item)
            finally:
                self._q.task_done()

    def _deliver(self, title, body, priority, tags, click, who=None) -> None:
        for attempt in range(len(self.retries) + 1):
            try:
                publish(self.server, self.topic, title, body, priority, tags, click, who=who)
                self.sent += 1
                self.last_error = ""
                return
            except NotifyError as e:
                self.last_error = str(e)
                status = getattr(e, "status", 0)
                if status and status != 429 and status < 500:      # refused for good (bad topic, too big)
                    break
                if attempt < len(self.retries):
                    self.sleep(self.retries[attempt])
        self.failed += 1
        self.log(f"notify: not sent ({self.last_error}): {title[:80]}")
