"""The hub's side of the station's logs: into CloudWatch Logs (/sizeup/station), and the admins' Logs view.

The station ships its journal over the link it already has (sizeup/cloud/logship.py, POST /api/station/logs, the
station token). The hub filters and scrubs it again (sizeup/logscrub.py: either pass alone is enough) and writes it
with the task role (PutLogEvents) to the department's own AWS account: no third-party log service, ever (the county
CAD rule: its details never leave the department).

    group        /sizeup/station (SIZEUP_STATION_LOG_GROUP), Infrequent Access class, retention 30 days by default
                 (Settings > Station > Logs, applied with PutRetentionPolicy); made by the department's setup script
                 (sizeup-aws step 25), never by the hub. Streams: <station>-<YYYY-MM-DD> (UTC), made here
    events       one JSON line each: {"unit", "level", "msg"}, so Logs Insights has unit and level as fields
    not set up   until that script has run (no group, or the task role may not write it) the hub says "not configured"
                 and answers the station {accepted: false, retry_s: 600}: the station keeps its spool (capped) and
                 tries again. Checked every CHECK_S and on the Logs view; nothing else on the hub depends on it
    query        GET /api/cad/logs (admins with a verified sign-in): Logs Insights over the station's group, the hub's
                 own (/ecs/sizeup-hub, SIZEUP_HUB_LOG_GROUP) or both, filtered by unit, level and text, newest first;
                 bounded: at most MAX_WINDOW_S of time, MAX_ROWS rows, one query at a time, MIN_GAP_S apart
                 (Logs Insights bills by the bytes scanned: a day of station logs is a few MB, a fraction of a cent)

Settings (the CAD settings' SECTION "station_logs", Settings > Station > Logs, admins, audited as System):
    on              true | false: the station ships at all
    level           debug | info | warning | error: the least level sent (default info: debug stays on the station)
    include         [units]: only these (empty: every sizeup unit)
    exclude         [units]: never these
    retention_days  one of RETENTION_DAYS (default 30): the group's retention
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import zlib
from datetime import datetime, timezone
from typing import Callable

from sizeup import aws, logscrub

SECTION = "station_logs"
DEFAULTS = {"on": True, "level": "info", "include": [], "exclude": [], "retention_days": 30}
LABELS = {"on": "Ship station logs", "level": "Least level", "include": "Only these units",
          "exclude": "Never these units", "retention_days": "Keep (days)"}
RETENTION_DAYS = (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365)     # CloudWatch's values (up to a year here)
UNIT = re.compile(r"[A-Za-z0-9@._:-]{1,64}")
KNOWN_UNITS = ("sizeup-station", "sizeup-whisper", "sizeup-trunk-recorder", "sizeup-update", "sizeup-health",
               "sizeup-adsb", "sizeup-ais", "sizeup-usbfs")
HUB_UNITS = ("hub", "cloudflared")            # the hub task's containers (its log streams' prefixes)
STATION_GROUP = "/sizeup/station"
HUB_GROUP = "/ecs/sizeup-hub"                 # deploy/aws/taskdef.json awslogs-group
TARGET = "Logs_20140328."
MAX_BODY = 4 * 1024 * 1024                    # a gzipped batch from the station
MAX_INFLATED = 16 * 1024 * 1024
MAX_PUT_EVENTS = 9000                         # CloudWatch: 10,000 events ...
MAX_PUT_BYTES = 900 * 1024                    # ... and 1,048,576 bytes (each event's UTF-8 + 26) per call
MAX_EVENT_BYTES = 200 * 1024
MAX_AGE_S = 13 * 86400                        # CloudWatch refuses events older than 14 days ...
MAX_AHEAD_S = 7200                            # ... or more than 2 h ahead
RETRY_S = 600                                 # not set up: the station tries again in this long
CHECK_S = 600.0
MAX_WINDOW_S = 7 * 86400
DEFAULT_WINDOW_S = 3600
MAX_ROWS, DEFAULT_ROWS = 500, 200
MAX_TEXT = 120
QUERY_WAIT_S = 20.0
MIN_GAP_S = 2.0
SETUP = "run sizeup-aws step 25 (25-station-logs.sh)"
ERR_RX = r"(?i)(traceback|exception|error|failed|failure|fatal|critical|crash|panic|killed)"
WARN_RX = (r"(?i)(traceback|exception|error|failed|failure|fatal|critical|crash|panic|killed|warn|refused|timed out"
           r"|timeout|retry|offline|backing off|giving up|stalled|stuck|dropped|degraded|restart|unreachable)")


class LogsError(RuntimeError):
    """A bad query (400) or one that can't run now (status)."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# -- settings -----------------------------------------------------------------------------------------------------

def config(stored: dict | None) -> dict:
    """The settings in effect: the stored ones over DEFAULTS (anything malformed: its default)."""
    s = stored if isinstance(stored, dict) else {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in s:
            try:
                out[k] = check_value(k, s[k])
            except ValueError:
                pass
    return out


def check_value(key: str, v):
    """A setting's value as kept, or ValueError with why."""
    if key == "on":
        if not isinstance(v, bool):
            raise ValueError("on: true or false")
        return v
    if key == "level":
        if v not in logscrub.LEVELS:
            raise ValueError(f"level: one of {', '.join(logscrub.LEVELS)}")
        return v
    if key in ("include", "exclude"):
        if isinstance(v, str):
            v = [x for x in re.split(r"[\s,]+", v) if x]
        if not isinstance(v, list) or len(v) > 50 or not all(isinstance(u, str) and UNIT.fullmatch(u) for u in v):
            raise ValueError(f"{key}: unit names (letters, digits, - _ . @ :), at most 50")
        return sorted(dict.fromkeys(u.removesuffix(".service") for u in v))
    if key == "retention_days":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or int(v) not in RETENTION_DAYS:
            raise ValueError(f"retention_days: one of {', '.join(map(str, RETENTION_DAYS))}")
        return int(v)
    raise ValueError(f"key: one of {', '.join(DEFAULTS)}")


def station_config(c: dict) -> dict:
    """What the station is told (its filter): on, level, include, exclude."""
    return {k: c[k] for k in ("on", "level", "include", "exclude")}


# -- CloudWatch ---------------------------------------------------------------------------------------------------

def _call(target: str, body: dict, reg: str, timeout: float = 15.0) -> dict:
    return aws.json_call("logs", TARGET + target, body, reg, timeout=timeout)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _safe_station(s) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]", "-", str(s or "station"))[:40]
    return s or "station"


class StationLogs:
    """The hub's station-log sink and Logs view. Never raises into the station's request or the hub's start."""

    def __init__(self, settings: Callable[[], dict], station_status: Callable[[], dict | None] = lambda: None,
                 log: Callable[[str], None] = print, call: Callable[..., dict] | None = None,
                 clock: Callable[[], float] = time.time, region: str = ""):
        self.settings, self.station_status, self.log, self.clock = settings, station_status, log, clock
        self._call = call or _call
        self.region = aws.region(region)
        self.group = (os.environ.get("SIZEUP_STATION_LOG_GROUP") or STATION_GROUP).strip()
        self.hub_group = (os.environ.get("SIZEUP_HUB_LOG_GROUP") or HUB_GROUP).strip()
        self.configured: bool | None = None          # None: not checked yet
        self.reason = "not checked yet"
        self.retention: int | None = None
        self.stored_bytes: int | None = None
        self.checked = 0.0
        self.streams: set[str] = set()
        self.units: set[str] = set(KNOWN_UNITS)
        self.day = ""
        self.today = {"lines": 0, "bytes": 0, "batches": 0, "dropped": 0}
        self.last_put: float | None = None
        self.last_error = ""
        self._lock = threading.Lock()
        self._query_lock = threading.Lock()
        self._last_query = 0.0
        self._said = ""

    def cfg(self) -> dict:
        try:
            return config(self.settings() or {})
        except Exception:  # noqa: BLE001 - the settings unreadable: the defaults
            return dict(DEFAULTS)

    # -- set up? ------------------------------------------------------------------------------------------------

    def _not_configured(self, why: str) -> None:
        self.configured, self.reason = False, why
        if self._said != why:
            self._said = why
            self.log(f"station logs: not configured ({why})")

    def check(self, force: bool = False) -> dict:
        """Is the group there and may we write it? Its retention brought to the setting. Every CHECK_S at most."""
        now = self.clock()
        if not force and self.checked and now - self.checked < CHECK_S:
            return self.status_core()
        self.checked = now
        try:
            d = self._call("DescribeLogGroups", {"logGroupNamePrefix": self.group, "limit": 5}, self.region)
        except aws.AwsError as e:
            self._not_configured(f"{e.code or 'error'}: {SETUP}" if e.code in ("AccessDeniedException", "NoCredentials")
                                 else f"{e.code or 'error'}")
            return self.status_core()
        g = next((x for x in d.get("logGroups") or [] if x.get("logGroupName") == self.group), None)
        if g is None:
            self._not_configured(f"no log group {self.group}: {SETUP}")
            return self.status_core()
        self.retention = g.get("retentionInDays")
        self.stored_bytes = g.get("storedBytes")
        want = self.cfg()["retention_days"]
        if self.retention != want:
            try:
                self._call("PutRetentionPolicy", {"logGroupName": self.group, "retentionInDays": want}, self.region)
                self.log(f"station logs: {self.group} kept {want} days (was {self.retention})")
                self.retention = want
            except aws.AwsError as e:
                self.last_error = f"retention: {e.code}"
        if self.configured is not True:
            self.log(f"station logs: writing to {self.group}")
        self.configured, self.reason, self._said = True, "", ""
        return self.status_core()

    # -- the station's batches ---------------------------------------------------------------------------------

    def ingest(self, raw: bytes | None, headers) -> tuple[int, dict]:
        """POST /api/station/logs: (HTTP status, answer). accepted: the station may delete the batch."""
        c = self.cfg()
        answer = {"ok": True, "accepted": False, "config": station_config(c)}
        if raw is None:
            return 413, {"error": "too large"}
        if (headers.get("Content-Encoding") or "").strip().lower() == "gzip":
            z = zlib.decompressobj(wbits=31)
            try:
                raw = z.decompress(raw, MAX_INFLATED)
            except zlib.error as e:
                return 400, {"error": f"bad gzip: {e}"}
            if z.unconsumed_tail:
                return 413, {"error": "too large"}
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            return 400, {"error": "not JSON"}
        if not isinstance(d, dict) or not isinstance(d.get("lines"), list):
            return 400, {"error": "{v, station, batch, lines: [[ms, unit, level, msg]]}"}
        if not c["on"]:
            return 200, dict(answer, accepted=True, reason="off in Settings")    # (dropped: the station stops reading)
        if self.configured is not True:
            self.check()
        if self.configured is not True:
            return 200, dict(answer, retry_s=RETRY_S, reason=f"hub: not configured ({self.reason})")
        events, dropped = self.events(d["lines"], c)
        try:
            self.put(_safe_station(d.get("station")), events)
        except aws.AwsError as e:
            self.last_error = f"{e.code}"[:200]
            if e.code in ("AccessDeniedException", "NoCredentials") or (
                    e.code == "ResourceNotFoundException" and "group" in str(e).lower()):
                self._not_configured(f"{e.code}: {SETUP}")
                return 200, dict(answer, retry_s=RETRY_S, reason=f"hub: not configured ({self.reason})")
            return 503, {"error": f"CloudWatch: {e.code}"}
        self._count(len(events), sum(len(e["message"]) for e in events), dropped)
        return 200, dict(answer, accepted=True, n=len(events))

    def events(self, lines: list, c: dict) -> tuple[list[dict], int]:
        """The batch's lines as CloudWatch events (filtered and scrubbed again here), oldest first; and how many
        were dropped."""
        now_ms = int(self.clock() * 1000)
        out, dropped = [], 0
        for r in lines:
            if not (isinstance(r, list) and len(r) == 4 and isinstance(r[0], (int, float)) and isinstance(r[1], str)
                    and isinstance(r[3], str)):
                dropped += 1
                continue
            ms, unit, lv, msg = int(r[0]), r[1][:64], r[2] if r[2] in logscrub.LEVELS else "info", r[3]
            if not UNIT.fullmatch(unit) or not (now_ms - MAX_AGE_S * 1000 < ms < now_ms + MAX_AHEAD_S * 1000):
                dropped += 1
                continue
            if (c["include"] and unit not in c["include"]) or unit in c["exclude"] or not logscrub.at_least(lv, c["level"]):
                dropped += 1
                continue
            clean = logscrub.scrub(msg)
            if clean is None:
                dropped += 1
                continue
            m = json.dumps({"unit": unit, "level": lv, "msg": clean}, ensure_ascii=False, separators=(",", ":"))
            if len(m.encode()) > MAX_EVENT_BYTES:
                m = m.encode()[:MAX_EVENT_BYTES].decode("utf-8", "ignore")
            out.append({"timestamp": ms, "message": m})
            if unit not in self.units and len(self.units) < 100:
                self.units.add(unit)
        out.sort(key=lambda e: e["timestamp"])
        return out, dropped

    def put(self, station: str, events: list[dict]) -> int:
        """PutLogEvents, one stream per station and UTC day, in calls within CloudWatch's limits."""
        by_day: dict[str, list[dict]] = {}
        for e in events:
            by_day.setdefault(_day(e["timestamp"]), []).append(e)
        n = 0
        for day, evs in sorted(by_day.items()):
            stream = f"{station}-{day}"
            i = 0
            while i < len(evs):
                size, j = 0, i
                while j < len(evs) and j - i < MAX_PUT_EVENTS and size + len(evs[j]["message"].encode()) + 26 <= MAX_PUT_BYTES:
                    size += len(evs[j]["message"].encode()) + 26
                    j += 1
                j = max(j, i + 1)
                self._put_stream(stream, evs[i:j])
                n += j - i
                i = j
        if n:
            self.last_put = self.clock()
        return n

    def _put_stream(self, stream: str, evs: list[dict]) -> None:
        body = {"logGroupName": self.group, "logStreamName": stream, "logEvents": evs}
        if stream not in self.streams:
            self._create_stream(stream)
        try:
            r = self._call("PutLogEvents", body, self.region, timeout=20.0)
        except aws.AwsError as e:
            if e.code != "ResourceNotFoundException" or "stream" not in str(e).lower():
                raise
            self.streams.discard(stream)
            self._create_stream(stream)
            r = self._call("PutLogEvents", body, self.region, timeout=20.0)
        rej = (r or {}).get("rejectedLogEventsInfo")
        if rej:
            self.last_error = f"rejected: {json.dumps(rej)[:160]}"

    def _create_stream(self, stream: str) -> None:
        try:
            self._call("CreateLogStream", {"logGroupName": self.group, "logStreamName": stream}, self.region)
        except aws.AwsError as e:
            if e.code != "ResourceAlreadyExistsException":
                raise
        self.streams.add(stream)
        if len(self.streams) > 64:
            self.streams = {stream}

    def _count(self, n: int, size: int, dropped: int) -> None:
        with self._lock:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if day != self.day:
                self.day, self.today = day, {"lines": 0, "bytes": 0, "batches": 0, "dropped": 0}
            self.today["lines"] += n
            self.today["bytes"] += size
            self.today["batches"] += 1
            self.today["dropped"] += dropped

    # -- the Logs view -----------------------------------------------------------------------------------------

    def status_core(self) -> dict:
        return {"configured": self.configured, "reason": self.reason, "retention_days": self.retention,
                "stored_bytes": self.stored_bytes, "checked": self.checked or None}

    def status(self) -> dict:
        """GET /api/cad/logs/status: set up or not, the settings, today's volume, the station's shipper."""
        self.check()
        st = self.station_status() or {}
        ship = st.get("logship") if isinstance(st, dict) else None
        return {**self.status_core(), "group": self.group, "hub_group": self.hub_group, "settings": self.cfg(),
                "today": dict(self.today, day=self.day), "last_put": self.last_put, "error": self.last_error,
                "station": ship if isinstance(ship, dict) else None,
                "units": sorted(self.units), "hub_units": list(HUB_UNITS), "levels": list(logscrub.LEVELS),
                "retention_choices": list(RETENTION_DAYS),
                "limits": {"max_window_s": MAX_WINDOW_S, "max_rows": MAX_ROWS}}

    def build(self, q: dict, now: float | None = None) -> dict:
        """The query's parts from GET parameters (checked): {groups, start, end, limit, query}. LogsError: why not."""
        now = self.clock() if now is None else now
        one = lambda k, d="": str((q.get(k) or [d])[0] if isinstance(q.get(k), list) else q.get(k, d) or d).strip()  # noqa: E731
        source = one("source", "station")
        if source not in ("station", "hub", "all"):
            raise LogsError("source: station, hub or all")
        unit = one("unit")
        if unit and not UNIT.fullmatch(unit):
            raise LogsError("unit: a unit name")
        lv = one("level", "")
        if lv and lv not in logscrub.LEVELS:
            raise LogsError(f"level: one of {', '.join(logscrub.LEVELS)}")
        text = one("q")
        if len(text) > MAX_TEXT or any(ord(ch) < 32 for ch in text):
            raise LogsError(f"q: at most {MAX_TEXT} characters on one line")
        try:
            end = float(one("end") or now)
            start = float(one("start") or end - DEFAULT_WINDOW_S)
            limit = int(one("limit") or DEFAULT_ROWS)
        except ValueError:
            raise LogsError("start, end: unix seconds; limit: a number") from None
        end = min(end, now + 60)
        if not start < end:
            raise LogsError("start must be before end")
        if end - start > MAX_WINDOW_S:
            raise LogsError(f"at most {MAX_WINDOW_S // 86400} days at a time")
        limit = max(1, min(MAX_ROWS, limit))
        if unit in HUB_UNITS:
            source = "hub"
        elif unit:
            source = "station"
        groups = {"station": [self.group], "hub": [self.hub_group], "all": [self.group, self.hub_group]}[source]
        f: list[str] = []
        if unit in HUB_UNITS:
            f.append(f"@logStream like /^{unit}\\//")
        elif unit:
            f.append(f'unit = "{unit}"')
        if lv and lv != "debug":
            want = [x for x in logscrub.LEVELS if logscrub.at_least(x, lv)]
            hub_rx = ERR_RX if lv == "error" else WARN_RX if lv == "warning" else ""
            st = "level in [" + ", ".join(f'"{x}"' for x in want) + "]"
            if source == "station":
                f.append(st)
            elif source == "hub":
                if hub_rx:
                    f.append(f"@message like /{hub_rx}/")
            elif hub_rx:
                f.append(f"((ispresent(level) and {st}) or (not ispresent(level) and @message like /{hub_rx}/))")
        if text:
            f.append(f"@message like /(?i){_rx_escape(text)}/")
        qs = "fields @timestamp, @message, @logStream, @log, unit, level"
        for x in f:
            qs += f"\n| filter {x}"
        qs += f"\n| sort @timestamp desc\n| limit {limit}"
        return {"groups": groups, "start": int(start), "end": int(end) + 1, "limit": limit, "query": qs,
                "source": source}

    def query(self, q: dict) -> dict:
        """GET /api/cad/logs: run the Insights query (bounded) and the rows, newest first."""
        b = self.build(q)
        if not self._query_lock.acquire(blocking=False):
            raise LogsError("a query is running: try again in a moment", 429)
        try:
            gap = self.clock() - self._last_query
            if gap < MIN_GAP_S:
                time.sleep(MIN_GAP_S - gap)
            self._last_query = self.clock()
            try:
                qid = self._call("StartQuery", {"logGroupNames": b["groups"], "startTime": b["start"],
                                                "endTime": b["end"], "queryString": b["query"],
                                                "limit": b["limit"]}, self.region)["queryId"]
            except aws.AwsError as e:
                raise LogsError(self._why(e), 503 if e.code not in ("MalformedQueryException",) else 400) from None
            except (KeyError, TypeError):
                raise LogsError("CloudWatch: no query id", 502) from None
            deadline = time.monotonic() + QUERY_WAIT_S
            wait = 0.4
            while True:
                try:
                    r = self._call("GetQueryResults", {"queryId": qid}, self.region)
                except aws.AwsError as e:
                    raise LogsError(self._why(e), 503) from None
                st = r.get("status")
                if st in ("Complete", "Failed", "Cancelled", "Timeout"):
                    break
                if time.monotonic() > deadline:
                    try:
                        self._call("StopQuery", {"queryId": qid}, self.region)
                    except aws.AwsError:
                        pass
                    raise LogsError("the query took too long: narrow the time or add a filter", 504)
                time.sleep(wait)
                wait = min(2.0, wait * 1.5)
            if st != "Complete":
                raise LogsError(f"CloudWatch: query {st}", 502)
        finally:
            self._query_lock.release()
        rows = [self._row(x) for x in r.get("results") or []]
        s = r.get("statistics") or {}
        return {"rows": rows, "source": b["source"], "start": b["start"], "end": b["end"], "limit": b["limit"],
                "scanned_bytes": s.get("bytesScanned"), "matched": s.get("recordsMatched"),
                "truncated": len(rows) >= b["limit"]}

    def _why(self, e: aws.AwsError) -> str:
        if e.code in ("AccessDeniedException", "NoCredentials"):
            return f"CloudWatch: {e.code} ({SETUP})"
        if e.code == "ResourceNotFoundException":
            return f"CloudWatch: no such log group ({SETUP})"
        return f"CloudWatch: {e.code or 'error'}"

    def _row(self, fields: list) -> dict:
        f = {x.get("field"): x.get("value") for x in fields if isinstance(x, dict)}
        try:
            t = datetime.strptime(str(f.get("@timestamp")), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            t = None
        lg = str(f.get("@log") or "")
        source = "station" if lg.endswith(":" + self.group) or lg == self.group else "hub"
        msg = str(f.get("@message") or "")
        if source == "station":
            try:
                d = json.loads(msg)
                unit, lv, msg = str(d.get("unit") or "?"), str(d.get("level") or "info"), str(d.get("msg") or "")
            except (ValueError, AttributeError):
                unit, lv = str(f.get("unit") or "?"), str(f.get("level") or "info")
        else:
            unit = str(f.get("@logStream") or "hub").split("/")[0]
            lv = logscrub.level(6, msg)
        shown = logscrub.scrub(msg)          # (the hub's own lines too: never a credential on screen)
        return {"t": t, "source": source, "unit": unit, "level": lv,
                "msg": shown if shown is not None else "[line withheld: it looked like a credential]"}


def _rx_escape(text: str) -> str:
    """Text as a Logs Insights regex literal's body: every character but letters, digits, _ and space escaped."""
    return "".join(ch if ch.isalnum() or ch in " _" else "\\" + ch for ch in text)
