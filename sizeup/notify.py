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

Two topics, by kind (route(), Pushes; user, 2026-11-01):

    ALERTS  [alerts] ntfy_topic / SIZEUP_ALERT_TOPIC (the hub: SSM /sizeup-hub/alert-topic): incident-level
            alarms: the emergency buttons (P25, MDC, FleetSync) and the fireground mayday (sizeup/cad/alarms.py),
            aircraft emergency squawks (sizeup/aircraft.py through the alarms), their acknowledgements, all
            clears and "Ended" notices
    STATUS  [alerts] status_topic / SIZEUP_STATUS_TOPIC (the hub: SSM /sizeup-hub/status-topic): the system's
            own health: station health (sizeup/cloud/health.py: silent, behind, update failed, a dongle gone, a
            job / whisper / trunk-recorder down, a stuck channel, the outbox, the disk, the whisper queue), Amazon
            Transcribe failing, its monthly budget at 80 % / 100 %, and their recovery notices

Only one of them set: every kind goes there (as before the split). Neither: no pushes. The app logs at startup
where each kind goes (Pushes.line(): "topic set (SSM)", "off", never a topic's name).

Settings > System > Push notifications (admins only) can change either topic and the server (Pushes, the CAD
settings' section "notify"): Settings win over env / SSM, which win over the config file; empty = that default;
Turn off = no pushes of that kind. Read at every send: no restart. A test push ("sizeup test: status topic")
goes to the topic in effect. The names are shown to admins only: never in the audit log (a fingerprint),
the activity, the settings export, the status endpoint or the logs.

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
        self.last_at, self.last_ok = 0.0, False     # the last push delivered or given up on: when, and whether it went
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
                self.last_at, self.last_ok = time.time(), True
                return
            except NotifyError as e:
                self.last_error = str(e)
                status = getattr(e, "status", 0)
                if status and status != 429 and status < 500:      # refused for good (bad topic, too big)
                    break
                if attempt < len(self.retries):
                    self.sleep(self.retries[attempt])
        self.failed += 1
        self.last_at, self.last_ok = time.time(), False
        self.log(f"notify: not sent ({self.last_error}): {title[:80]}")


# -- the topics in effect, live (Settings over env / SSM over the config file) ----------------------------------------
# user, 2026-11-01: "make the topics changeable in settings?". The CAD settings' section "notify" (sizeup/cad/settings.py,
# Settings > System > Push notifications, admins only): key "alerts" / "status" -> {"topic": name} or {"off": true},
# "server" -> the ntfy server. Not there: the config's (env / SSM over the file). Read at every send: a change takes
# effect on the next push, no restart. Never in the settings export (a topic's name is its only secret).

SECTION = "notify"
LABELS = {ALERTS: "Alerts topic", STATUS: "Status & health topic"}
SOURCES = {"settings": "Settings", "ssm": "SSM", "env": "environment", "config": "config file", "default": "default",
           "fallback": "shared", "off": "turned off in Settings", "none": "not set"}
TEST_TITLE = "sizeup test: {kind} topic"


def other(kind: str) -> str:
    return STATUS if kind == ALERTS else ALERTS


def fingerprint(topic: str) -> str:
    """A topic as the audit log and the logs show it: "topic #3f9c27", never the name (anyone signed in reads those)."""
    import hashlib
    return "topic #" + hashlib.sha256(topic.strip().encode()).hexdigest()[:6] if topic.strip() else "none"


class KindNotifier:
    """One kind's pushes (ALERTS, STATUS) through Pushes: the topic in effect is looked up at every send, so a
    change in Settings takes effect on the next push. What Notifier offers its callers (send, enabled, server,
    topic, sent, failed, last_error, flush), for sizeup/cad/alarms.py and sizeup/cloud/health.py."""

    def __init__(self, pushes: Pushes, kind: str):
        self.pushes, self.kind = pushes, kind

    def current(self) -> Notifier | None:
        return self.pushes.notifier_now(self.kind)

    @property
    def enabled(self) -> bool:
        return bool(self.pushes.effective(self.kind)["topic"])

    @property
    def topic(self) -> str:
        return self.pushes.effective(self.kind)["topic"]

    @property
    def server(self) -> str:
        return self.pushes.server()[0]

    def _of(self, name: str, default):
        n = self.current()
        return getattr(n, name) if n is not None else default

    sent = property(lambda self: self._of("sent", 0))
    failed = property(lambda self: self._of("failed", 0))
    last_error = property(lambda self: self._of("last_error", ""))

    def send(self, title: str, body: str, priority: int | str = 3, tags: Iterable[str] = (), click: str = "",
             who=None) -> bool:
        n = self.current()
        return n.send(title, body, priority, tags, click, who=who) if n is not None else False

    def flush(self, timeout: float = 10.0) -> bool:
        return all(n.flush(timeout) for n in self.pushes.made())


class Pushes:
    """Where each kind of push goes now, and the Notifiers that send them: one per (server, topic), so kinds that
    share a topic share its queue, in order. defaults: the config's topics with where each came from ("ssm",
    "env", "config"); settings(): the CAD settings' SECTION ({} without); also_forbidden: the configured
    [prealert] topics (refused by exact name like Tapout's)."""

    def __init__(self, server: str = DEFAULT_SERVER, alert_topic: str = "", status_topic: str = "",
                 sources: dict | None = None, server_source: str = "default", settings: Callable[[], dict] | None = None,
                 also_forbidden: Iterable[str] = (), log: Callable[[str], None] = print, **kw):
        src = sources or {}
        self.defaults = {ALERTS: ((alert_topic or "").strip(), src.get(ALERTS) or "config"),
                         STATUS: ((status_topic or "").strip(), src.get(STATUS) or "config")}
        self.default_server = ((server or DEFAULT_SERVER).rstrip("/"), server_source)
        self.settings = settings or (lambda: {})
        self.also_forbidden = tuple(str(x) for x in also_forbidden if str(x).strip())
        self.log, self.kw = log, kw
        self.publish = publish                    # the test push (tests: a fake)
        self.tests: dict[str, dict] = {}          # kind -> the last test push {at, ok, error}
        self._made: dict[tuple[str, str], Notifier] = {}
        self._lock = threading.Lock()
        self.kinds = {k: KindNotifier(self, k) for k in KINDS}

    # -- what is in effect ---------------------------------------------------------------------------------------

    def _ov(self) -> dict:
        try:
            d = self.settings() or {}
        except Exception as e:  # noqa: BLE001 - the CAD's settings unreadable: the config's topics
            self.log(f"notify: Settings unreadable ({type(e).__name__}): the config's topics")
            d = {}
        return d if isinstance(d, dict) else {}

    def problem(self, topic: str) -> str:
        return topic_problem(topic, self.also_forbidden)

    def server(self, ov: dict | None = None) -> tuple[str, str]:
        """(the ntfy server, where it came from)."""
        s = (self._ov() if ov is None else ov).get("server")
        if isinstance(s, str) and s.strip() and not server_problem(s.strip()):
            return s.strip().rstrip("/"), "settings"
        return self.default_server

    def _own(self, kind: str, ov: dict) -> tuple[str, str]:
        v = ov.get(kind)
        if isinstance(v, dict):
            if v.get("off"):
                return "", "off"
            t = str(v.get("topic") or "").strip()
            if t and not self.problem(t):
                return t, "settings"
        t, src = self.defaults[kind]
        return (t, src) if t and not self.problem(t) else ("", "none")

    def effective(self, kind: str, ov: dict | None = None) -> dict:
        """{topic ("" = no push), source: settings | ssm | env | config | fallback (the other kind's) | off | none}."""
        ov = self._ov() if ov is None else ov
        t, src = self._own(kind, ov)
        if src == "none":
            ot, _ = self._own(other(kind), ov)
            if ot:
                return {"topic": ot, "source": "fallback"}
        return {"topic": t, "source": src}

    def line(self) -> str:
        """Where each kind goes, for the log: never a topic's name."""
        ov = self._ov()
        eff = {k: self.effective(k, ov) for k in KINDS}
        if not any(e["topic"] for e in eff.values()):
            return "notify: no pushes" + ("" if any(e["source"] == "off" for e in eff.values())
                                          else " (no [alerts] ntfy_topic / status_topic set)")

        def say(k: str) -> str:
            e = eff[k]
            if e["source"] == "fallback":
                return f"the {other(k)} topic (no {k} topic set)"
            if e["source"] == "off":
                return "off (Settings)"
            return f"topic set ({SOURCES[e['source']]})" if e["topic"] else "off"
        return f"notify: alerts → {say(ALERTS)}, status → {say(STATUS)}"

    # -- sending ------------------------------------------------------------------------------------------------

    def notifier(self, kind: str) -> KindNotifier:
        return self.kinds[kind]

    def notifier_now(self, kind: str) -> Notifier | None:
        ov = self._ov()
        t = self.effective(kind, ov)["topic"]
        return self._notifier(self.server(ov)[0], t) if t else None

    def _notifier(self, server: str, topic: str) -> Notifier:
        with self._lock:
            n = self._made.get((server, topic))
            if n is None:
                n = self._made[(server, topic)] = Notifier(server, topic, self.log, **self.kw)
            return n

    def made(self) -> list[Notifier]:
        with self._lock:
            return list(self._made.values())

    def test(self, kind: str) -> dict:
        """A clearly marked test push to the kind's topic in effect, now: {at, ok, error}. NotifyError: no topic."""
        e = self.effective(kind)
        if not e["topic"]:
            raise NotifyError(f"no {kind} topic in effect ({SOURCES[e['source']]}): nothing to test")
        srv = self.server()[0]
        out = {"at": time.time(), "ok": True, "error": "", "source": e["source"]}
        try:
            self.publish(srv, e["topic"], TEST_TITLE.format(kind=kind),
                         f"A test push from Settings > System > Push notifications ({LABELS[kind]}). Nothing has happened.",
                         priority=2, tags=("test_tube", "sizeup"))
        except NotifyError as x:
            out.update(ok=False, error=str(x))
        self.tests[kind] = out
        return out

    # -- Settings > System > Push notifications (admins only: the names) ------------------------------------------

    def page(self) -> dict:
        ov = self._ov()
        srv, ssrc = self.server(ov)
        kinds = []
        for k in (STATUS, ALERTS):
            e = self.effective(k, ov)
            with self._lock:
                n = self._made.get((srv, e["topic"])) if e["topic"] else None
            mine = ov.get(k) if isinstance(ov.get(k), dict) else {}
            dt, dsrc = self.defaults[k]
            kinds.append({"kind": k, "label": LABELS[k], "topic": e["topic"], "source": e["source"],
                          "source_text": SOURCES[e["source"]] + (f" (the {other(k)} topic)" if e["source"] == "fallback" else ""),
                          "settings": {"topic": str(mine.get("topic") or ""), "off": bool(mine.get("off"))},
                          "default": {"topic": dt, "source": dsrc if dt else "none", "source_text": SOURCES[dsrc if dt else "none"]},
                          "last": ({"at": n.last_at, "ok": n.last_ok, "error": n.last_error, "sent": n.sent, "failed": n.failed}
                                   if n is not None and n.last_at else None),
                          "test": self.tests.get(k)})
        return {"kinds": kinds, "server": {"value": srv, "source": ssrc, "settings": str(ov.get("server") or ""),
                                           "default": self.default_server[0]},
                "forbidden": sorted(FORBIDDEN | set(self.also_forbidden)), "min_len": MIN_TOPIC_LEN, "max_len": 64,
                "line": self.line()}
