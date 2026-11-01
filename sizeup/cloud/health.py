"""Station health (GitHub #79, #35): know when the station is behind, updating, failed or quiet, and say so.

The station (StationLink.status(), every 5 s with the rest of its state post) adds "health": this machine's
CPU, memory, disk, temperature, when sizeup and the machine last started, and each radio program as it
runs (station_metrics()). A station from before this sends none: the hub then works from its radio and
SDR rows alone.

The hub (StationHealth, on HubApp) builds one `station_health` structure from what the station sent (model())
and checks it every TICK_S for these conditions, each with a text, a severity, a start time, and a recovery
notice when it clears:

    silent        no state post for [alerts] silent_min (3)                                critical
    behind        on another build than the hub's for behind_min (15)                      warning
    update        the update to the hub's build failed or was rolled back                  warning (critical:
                                                                                           the rollback failed too)
    sdr:<serial>  a dongle the station had is gone (unplugged)                             critical
    job:<job>     a dongle's job (a channel) not running for JOB_GRACE_S                   critical
    whisper       speech-to-text (whisper-server) down for down_min (2)                    warning
    recorder      trunk-recorder down for down_min while its dongle job is on              critical
    stuck:<chan>  a channel's squelch open without a break (#116's flag)                   warning
    outbox        events waiting on the station for outbox_min (5): it couldn't reach the  warning
                  hub (invisible here until it comes back: worked out from the age of the
                  oldest event it reports)
    disk          the station's disk over disk_pct (90) full                               warning (critical
                                                                                           from DISK_CRITICAL)
    stt_backlog   the station's whisper queue: its oldest live clip waiting STT_BACKLOG_S   warning
                  (10 min) or more, or clips dropped / skipped (sizeup/stt.py SttQueue, #195)
    transcribe    Amazon Transcribe on the hub failing long enough that its breaker is     warning
                  open (sizeup/transcribe.py, #195): AWS's reason (timed out, AccessDenied,
                  credentials, throttled, billing); the AWS channels run the station's
                  whisper meanwhile. Decided on the hub's own data, the station silent or not
    aws_budget80  this month's Amazon Transcribe cost at BUDGET_WARN (80 %) of the limit   warning
    aws_budget    ... at the limit: the AWS channels use the station's whisper (pages and   critical
                  Fire/EMS 15 keep AWS within the reserve, sizeup/transcribe.py)

While the station is silent (or before it first reports) the other conditions keep the state they had:
nothing starts or clears on old data. Per-channel silence (nothing heard for much longer than that channel
usually goes, from its own gaps between transmissions) is a hint in the panel, never an alert.

Each alert is logged once when it starts and once when it clears, shown as a banner on /cad and /monitor
(the CAD state's cloud.alerts, static/health.js), and pushed to the status topic ([alerts] status_topic, else
ntfy_topic: sizeup/notify.py route()): at most one push per condition per repeat_min (15), plus its recovery (only for an episode that was pushed).
Alerts and 24 h of their history are kept in hub.sqlite (meta "health"), so a redeploy neither forgets nor
repeats them.

    GET /api/cad/health        the panel (Settings > System > Station health): the model, the alerts, the
                               history, the thresholds; anyone signed in (Cloudflare Access on the hub)
    GET /api/station/status    the deploy check (tools/deploy_check.py): the hub's build, the station's, its
                               update state and last update line, the alerts. Read only: the station token
                               or the status token (status_token()), which can do nothing else

    python -m sizeup.cloud.health status-token     the status token for SIZEUP_STATION_TOKEN (from the env)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from sizeup.cloud.autoupdate import same_build, short

TICK_S = 5.0
JOB_GRACE_S = 60.0          # a dongle's job may be down this long (a restart, a switch) before it is an alert
DISK_CRITICAL = 97.0
HISTORY_S = 24 * 3600.0
HISTORY_MAX = 500
GAPS_MAX = 200              # per channel: the last this many gaps between transmissions
GAPS_MIN = 10               # ... at least this many before a silence hint
HINT_FACTOR = 3.0           # silent this many times its usual (90th percentile) gap
HINT_MIN_S = 30 * 60.0      # and at least this long
SAVE_GAPS_S = 600.0
NOT_CHANNELS = ("STT", "Voice", "Hub", "Station", "trunk-recorder")
STATUS_KEY = b"sizeup-status-token-v1"
P25_JOB = "Fire/EMS 15"
STT_BACKLOG_S = 600.0       # the station's whisper: its oldest live clip waiting this long is an alert (#195)
BUDGET_WARN = 0.8           # this month's AWS cost at this share of the limit: a warning (and a push)


def status_token(station_token: str, configured: str = "") -> str:
    """The read-only bearer for GET /api/station/status: [cloud] status_token / SIZEUP_STATUS_TOKEN when set,
    else derived from the station token (HMAC-SHA256), so CI never holds a token that can post events."""
    if configured:
        return configured
    if not station_token:
        return ""
    return hmac.new(station_token.encode(), STATUS_KEY, hashlib.sha256).hexdigest()


def _hhmm(t: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(t)) if t else "?"


def _dur(s: float) -> str:
    s = max(0, int(s))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    return f"{s // 3600} h {(s % 3600) // 60} min"


# -- the station's side --------------------------------------------------------------------------------------

def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


class Metrics:
    """This machine, cheaply, for the state post every 5 s: stdlib only; Linux reads /proc and /sys, macOS
    (and anything else) gives what it can (load, disk) and None for the rest."""

    def __init__(self, disk_path: str = ".", proc: str = "/proc", sys_dir: str = "/sys"):
        self.disk_path, self.proc, self.sys = disk_path or ".", proc, sys_dir
        self._cpu: tuple[int, int] | None = None
        self._boot: float | None = None
        self._booted = False
        self.started = self._process_start() or time.time()

    def _btime(self) -> float | None:
        m = re.search(r"^btime (\d+)", _read(f"{self.proc}/stat"), re.M)
        return float(m.group(1)) if m else None

    def _process_start(self) -> float | None:
        """When this process started: /proc/self/stat's starttime (ticks after boot)."""
        raw, boot = _read(f"{self.proc}/self/stat"), self._btime()
        try:
            ticks = int(raw.rsplit(")", 1)[1].split()[19])
            return boot + ticks / os.sysconf("SC_CLK_TCK") if boot else None
        except (IndexError, ValueError, OSError, AttributeError):
            return None

    def boot(self) -> float | None:
        if not self._booted:
            self._booted = True
            self._boot = self._btime()
            if self._boot is None and sys.platform == "darwin":
                try:
                    out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=2).stdout
                    m = re.search(r"sec = (\d+)", out)
                    self._boot = float(m.group(1)) if m else None
                except (OSError, subprocess.SubprocessError):
                    self._boot = None
        return self._boot

    def cpu_pct(self) -> float | None:
        """Busy share of all CPUs since the last call (Linux /proc/stat); None the first time or elsewhere."""
        line = _read(f"{self.proc}/stat").split("\n", 1)[0].split()
        if not line or line[0] != "cpu":
            return None
        try:
            v = [int(x) for x in line[1:]]
        except ValueError:
            return None
        idle, total = v[3] + (v[4] if len(v) > 4 else 0), sum(v[:8])
        prev, self._cpu = self._cpu, (idle, total)
        if prev is None or total <= prev[1]:
            return None
        return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)

    def mem(self) -> tuple[float | None, int | None]:
        info = dict(re.findall(r"^(\w+):\s+(\d+) kB", _read(f"{self.proc}/meminfo"), re.M))
        try:
            total, avail = int(info["MemTotal"]), int(info["MemAvailable"])
        except (KeyError, ValueError):
            return None, None
        return (round(100.0 * (1 - avail / total), 1) if total else None), total * 1024

    def temp_c(self) -> float | None:
        """The hottest thermal zone (a Pi, a NUC); None without any (a VM, WSL, macOS)."""
        temps = []
        for p in Path(self.sys, "class", "thermal").glob("thermal_zone*/temp"):
            try:
                t = int(p.read_text().strip())
            except (OSError, ValueError):
                continue
            if 0 < t < 150000:
                temps.append(t / 1000.0)
        return round(max(temps), 1) if temps else None

    def sample(self) -> dict:
        try:
            du = shutil.disk_usage(self.disk_path)
            disk = {"disk_pct": round(100.0 * du.used / du.total, 1) if du.total else None,
                    "disk_free": du.free, "disk_total": du.total}
        except OSError:
            disk = {"disk_pct": None, "disk_free": None, "disk_total": None}
        try:
            load1 = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            load1 = None
        mem_pct, mem_total = self.mem()
        return {"started": self.started, "boot": self.boot(), "host": socket.gethostname()[:64],
                "cpu_pct": self.cpu_pct(), "load1": load1, "cpus": os.cpu_count(), "mem_pct": mem_pct,
                "mem_total": mem_total, "temp_c": self.temp_c(), "disk_path": str(self.disk_path)[:200], **disk}


def station_metrics(app, metrics: Metrics) -> dict:
    """status["health"] in the station's state post: the machine, and each radio program as it really runs
    (the rows say "off" for a job no dongle has; here every job with whether it is expected to run)."""
    out = metrics.sample()
    shared = bool(getattr(app, "shared", False))
    assigned = set((getattr(app, "assign", None) or {}).values())
    jobs = {}
    for r in getattr(app, "receivers", []) or []:
        try:
            st = r.status()
        except Exception:  # a receiver mid-restart: next time
            continue
        jobs[st.get("label", "?")] = {"state": st.get("state"), "restarts": st.get("restarts", 0),
                                      "detail": str(st.get("detail") or "")[:200],
                                      "expected": (not shared) or st.get("label") in assigned}
    rec = getattr(app, "recorder", None)
    if rec is not None and getattr(app, "p25", None) is not None:
        rs = rec.status()
        out["recorder"] = {"state": rs.get("state"), "restarts": rs.get("restarts", 0),
                           "detail": str(rs.get("detail") or "")[:200],
                           "expected": (not shared) or P25_JOB in assigned}
    w = getattr(app, "whisper", None)
    if w is not None:
        out["whisper"] = {"state": getattr(w, "state", None), "detail": str(getattr(w, "detail", "") or "")[:200]}
    stt = getattr(app, "stt", None)
    if stt is not None and hasattr(stt, "stats"):
        try:
            out["stt_queue"] = stt.stats()          # depth, the oldest live clip's wait, what was dropped (#195)
        except Exception:
            pass
    out["jobs"] = jobs
    return out


# -- the hub's side ----------------------------------------------------------------------------------------------

def _p90(xs: list[float]) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))] if s else 0.0


class StationHealth:
    """On the hub: the model, the alerts, their history and pushes. hub: sizeup/cloud/hub.py Hub; db: its
    MonitorDb (meta "health", "health_gaps"); notifier: sizeup/notify.py Notifier (or None: no push)."""

    def __init__(self, hub, cfg, db=None, notifier=None, log: Callable[[str], None] = print,
                 clock: Callable[[], float] = time.time):
        self.hub, self.db, self.notifier, self.log, self.clock = hub, db, notifier, log, clock
        self.silent_s = cfg.alerts_silent_min * 60
        self.behind_s = cfg.alerts_behind_min * 60
        self.down_s = cfg.alerts_down_min * 60
        self.outbox_s = cfg.alerts_outbox_min * 60
        self.disk_pct = cfg.alerts_disk_pct
        self.repeat_s = cfg.alerts_repeat_min * 60
        self.public_url = (cfg.cloud_public_url or "").rstrip("/")
        self._lock = threading.RLock()
        self.active: dict[str, dict] = {}
        self.history: list[dict] = []
        self.last_push: dict[str, float] = {}
        self._first: dict[str, float] = {}           # when each held-off condition was first seen
        self._heard: dict[str, float] = {}           # each channel's last_heard as last seen
        self.gaps: dict[str, list[float]] = {}
        self._gaps_saved = 0.0
        self._load()

    # -- persistence ---------------------------------------------------------------------------------------

    def _load(self) -> None:
        if self.db is None:
            return
        try:
            d = json.loads(self.db.get("health", "") or "{}")
            g = json.loads(self.db.get("health_gaps", "") or "{}")
        except ValueError:
            d, g = {}, {}
        if isinstance(d, dict):
            self.active = {k: v for k, v in (d.get("active") or {}).items() if isinstance(v, dict)}
            self.history = [h for h in d.get("history") or [] if isinstance(h, dict)]
            self.last_push = {k: float(v) for k, v in (d.get("last_push") or {}).items() if isinstance(v, (int, float))}
        if isinstance(g, dict):
            self.gaps = {str(k): [float(x) for x in v if isinstance(x, (int, float))][-GAPS_MAX:]
                         for k, v in g.items() if isinstance(v, list)}
            self._heard = {str(k): float(v) for k, v in (g.get("_heard") or {}).items()} if isinstance(g.get("_heard"), dict) else {}
            self.gaps.pop("_heard", None)

    def _save(self) -> None:
        if self.db is None:
            return
        try:
            self.db.put("health", json.dumps({"active": self.active, "history": self.history[-HISTORY_MAX:],
                                              "last_push": self.last_push}, separators=(",", ":")))
        except Exception as e:  # never in the way of the alerts themselves
            self.log(f"health: can't save the alerts: {type(e).__name__}: {e}")

    def _save_gaps(self, now: float) -> None:
        if self.db is None or now - self._gaps_saved < SAVE_GAPS_S:
            return
        self._gaps_saved = now
        try:
            self.db.put("health_gaps", json.dumps(dict(self.gaps, _heard=self._heard), separators=(",", ":")))
        except Exception:
            pass

    # -- what the station sent -------------------------------------------------------------------------------

    def observe(self, status: dict | None) -> None:
        """A state post with a status: each channel's gap since it was last heard, for the silence hints."""
        if not isinstance(status, dict):
            return
        with self._lock:
            for r in status.get("radios") or []:
                if not isinstance(r, dict) or r.get("label") in NOT_CHANNELS:
                    continue
                label, heard = str(r.get("label") or ""), r.get("last_heard")
                if not label or not isinstance(heard, (int, float)) or heard <= 0:
                    continue
                prev = self._heard.get(label)
                if prev is not None and heard > prev + 1:
                    self.gaps.setdefault(label, []).append(round(heard - prev, 1))
                    del self.gaps[label][:-GAPS_MAX]
                if prev is None or heard > prev:
                    self._heard[label] = float(heard)

    def _station_now(self, now: float) -> float:
        return now - (self.hub.skew or 0.0)

    def model(self, now: float | None = None) -> dict:
        """station_health: everything the panel shows, from the station's last report."""
        now = self.clock() if now is None else now
        hub, st = self.hub, self.hub.status or {}
        hl = st.get("health") if isinstance(st.get("health"), dict) else {}
        snow = self._station_now(now)
        last_state = hub.last_state
        out: dict = {"at": now, "reported": bool(hub.status), "health_reported": bool(hl)}
        out["station"] = {"name": st.get("name") or "", "id": st.get("id") or "", "connected": hub.connected(),
                          "last_seen": hub.last_seen, "last_state": last_state,
                          "silent_s": round(now - last_state, 1) if last_state else None,
                          "skew_s": round(hub.skew, 1), "hub_started": hub.started,
                          "silent": self._silent(now)[0] is True}
        v = st.get("version") if isinstance(st.get("version"), dict) else {}
        desc = hub.version() or {"state": "unknown", "text": "the station doesn't report its version"}
        upd = v.get("update") if isinstance(v.get("update"), dict) else {}
        out["build"] = {"station": v.get("build") or "", "hub": hub.build, "state": desc["state"], "text": desc["text"],
                        "update": {k: upd.get(k) for k in ("state", "since", "to", "from", "paused") if k in upd},
                        "last": upd.get("last") if isinstance(upd.get("last"), dict) else None,
                        "failed": upd.get("failed") if isinstance(upd.get("failed"), dict) else {},
                        "behind_since": self._first.get("behind")}
        ob = st.get("outbox") if isinstance(st.get("outbox"), dict) else {}
        oldest = ob.get("oldest") if isinstance(ob.get("oldest"), (int, float)) else None
        out["outbox"] = {"pending": int(ob.get("pending") or 0), "oldest": oldest + hub.skew if oldest else None,
                         "age_s": round(snow - oldest, 1) if oldest and ob.get("pending") else None,
                         "dropped_audio": int(ob.get("dropped_audio") or 0)}
        rows = [r for r in st.get("radios") or [] if isinstance(r, dict)]
        jobs = hl.get("jobs") if isinstance(hl.get("jobs"), dict) else {}
        sdr = st.get("sdr") if isinstance(st.get("sdr"), dict) else None
        dongles = []
        for d in (sdr or {}).get("sdrs") or []:
            if not isinstance(d, dict):
                continue
            job = str(d.get("mode") or "Off")
            js = jobs.get(job) or (hl.get("recorder") if job == P25_JOB else None) or \
                next((r for r in rows if r.get("label") == job), None) or {}
            dongles.append({"serial": str(d.get("serial") or ""), "name": str(d.get("name") or d.get("serial") or ""),
                            "present": bool(d.get("present")), "job": job, "set_by": d.get("set_by"),
                            "held_by": d.get("held_by"),
                            "running": bool(d.get("present")) and job != "Off" and js.get("state") == "ok",
                            "state": js.get("state") if job != "Off" else "off", "restarts": js.get("restarts", 0),
                            "detail": str(js.get("detail") or "")[:200]})
        out["dongles"] = dongles
        out["shared"] = bool(sdr and sdr.get("shared"))
        chans = []
        for r in rows:
            label = str(r.get("label") or "")
            if label in NOT_CHANNELS:
                continue
            heard = r.get("last_heard") if isinstance(r.get("last_heard"), (int, float)) and r.get("last_heard") else None
            gaps = self.gaps.get(label) or []
            typical = _p90(gaps) if len(gaps) >= GAPS_MIN else None
            quiet = round(snow - heard, 1) if heard else None
            hint = ""
            if typical is not None and quiet is not None and r.get("state") == "ok" \
                    and quiet > max(HINT_FACTOR * typical, HINT_MIN_S):
                hint = (f"nothing heard for {_dur(quiet)}; it usually hears something within {_dur(typical)}: "
                        "check its antenna and dongle")
            chans.append({"label": label, "mhz": r.get("mhz"), "state": r.get("state"), "word": r.get("word") or "",
                          "detail": str(r.get("detail") or "")[:200], "open": bool(r.get("open")),
                          "open_s": r.get("open_s"), "stuck": bool(r.get("stuck")), "quieting_db": r.get("quieting_db"),
                          "restarts": r.get("restarts", 0), "last_heard": heard + hub.skew if heard else None,
                          "quiet_s": quiet, "typical_gap_s": typical, "gaps": len(gaps), "hint": hint})
        out["channels"] = chans
        stt = next((r for r in rows if r.get("label") == "STT"), None)
        wh = hl.get("whisper") if isinstance(hl.get("whisper"), dict) else (
            {"state": stt.get("state"), "detail": stt.get("detail")} if stt else None)
        rec = hl.get("recorder") if isinstance(hl.get("recorder"), dict) else None
        out["processes"] = {"whisper": dict(wh, down_since=self._first.get("whisper")) if wh else None,
                            "recorder": dict(rec, down_since=self._first.get("recorder")) if rec else None}
        sq = hl.get("stt_queue") if isinstance(hl.get("stt_queue"), dict) else None
        out["stt_queue"] = {k: sq.get(k) for k in ("queued", "urgent", "live", "rerun", "stale", "oldest_s", "dropped",
                                                   "skipped", "last_drop", "max", "working")} if sq else None
        started = hl.get("started")
        out["system"] = {k: hl.get(k) for k in ("host", "cpu_pct", "load1", "cpus", "mem_pct", "mem_total", "temp_c",
                                                 "disk_pct", "disk_free", "disk_total", "disk_path")}
        out["system"].update(
            started=started + hub.skew if isinstance(started, (int, float)) else None,
            uptime_s=round(snow - started) if isinstance(started, (int, float)) else None,
            boot=hl["boot"] + hub.skew if isinstance(hl.get("boot"), (int, float)) else None)
        out["hints"] = [{"channel": c["label"], "text": c["hint"]} for c in chans if c["hint"]]
        out["transcribe"] = self._transcribe()
        return out

    def _transcribe(self) -> dict | None:
        """Amazon Transcribe's health on the hub (HubApp.aws_health()), None when it isn't set up here."""
        fn = getattr(getattr(self.hub, "app", None), "aws_health", None)
        if fn is None:
            return None
        try:
            h = fn()
        except Exception as e:  # never in the way of the rest
            return {"on": True, "state": "unknown", "text": f"health: {type(e).__name__}: {e}"}
        return h if isinstance(h, dict) and h.get("on") else None

    # -- conditions ------------------------------------------------------------------------------------------------

    def _silent(self, now: float) -> tuple[bool | None, float | None]:
        """(silent?, since when): None = can't tell yet (the hub just started and nobody reported)."""
        last = self.hub.last_state
        if last is not None:
            return (now - last >= self.silent_s), last
        if now - self.hub.started >= self.silent_s:
            return True, self.hub.started
        return None, None

    def conditions(self, m: dict, now: float) -> tuple[dict[str, dict], bool]:
        """(the conditions holding now {key: alert}, whether the station's data can be judged at all: False
        while it's silent or hasn't reported, when only "silent" itself is decided)."""
        name = m["station"]["name"] or "the station"
        out: dict[str, dict] = {}
        tr = m.get("transcribe")                                 # the hub's own: decided even while the station is silent
        if tr and tr.get("state") in ("down", "retrying"):
            out["transcribe"] = {"severity": "warning", "title": "Amazon Transcribe failing",
                                 "text": f"{tr.get('text') or ''}: the AWS channels use the station's whisper meanwhile",
                                 "since": float(tr.get("failing_since") or now)}
        bud = (tr or {}).get("budget") or {}
        pct = bud.get("pct")
        if isinstance(pct, (int, float)) and pct >= BUDGET_WARN * 100:
            spent = f"${bud.get('usd', 0):.2f} of the ${bud.get('limit_usd', 0):g} limit in {bud.get('month', '')}"
            out["aws_budget80"] = {"severity": "warning", "title": f"Amazon Transcribe at {BUDGET_WARN:.0%} of its monthly limit",
                                   "text": f"{spent}: at the limit the AWS channels go to the station's whisper",
                                   "since": float(bud.get("warn_since") or now)}
            if pct >= 100:
                out["aws_budget"] = {"severity": "critical", "title": "Amazon Transcribe monthly limit reached",
                                     "text": f"{spent}: the AWS channels use the station's whisper until next month or a "
                                             "higher limit (Settings > Radios)" + (f"; {bud['reserve_text']}"
                                                                                  if bud.get("reserve_text") else ""),
                                     "since": float(bud.get("limit_since") or now)}
        silent, since = self._silent(now)
        if silent:
            out["silent"] = {"severity": "critical", "title": "Station silent",
                             "text": (f"no word from {name} since {_hhmm(since)}: radios, speech-to-text and what it "
                                      "hears are not reaching the hub" if self.hub.last_state else
                                      f"no station has reported since the hub started ({_hhmm(since)})"),
                             "since": since}
        if silent is None:
            out["silent"] = None                         # undecided: keep as it is
        if silent or not m["reported"]:
            if out.get("silent"):
                out["silent"]["who"] = self._who("silent", m)
            return out, False

        seen: set[str] = set()

        def held(key: str, hold_s: float, alert: dict, since: float | None = None) -> None:
            seen.add(key)
            if key in self.active:                       # on before (a hub restart): keep counting from then
                self._first.setdefault(key, float(self.active[key]["since"]))
            first = self._first.setdefault(key, now if since is None else since)
            if now - first >= hold_s:
                out[key] = dict(alert, since=first)

        b = m["build"]
        if b["state"] == "failed" or (isinstance(b["last"], dict) and b["last"].get("result") == "rollback failed"
                                      and same_build(b["last"].get("to"), b["hub"])):
            crit = isinstance(b["last"], dict) and b["last"].get("result") == "rollback failed"
            out["update"] = {"severity": "critical" if crit else "warning",
                             "title": "Station update failed" + (" and its rollback too" if crit else ""),
                             "text": b["text"] + (f" · {b['last']['line']}" if isinstance(b["last"], dict)
                                                  and b["last"].get("line") else ""),
                             "since": float((b["last"] or {}).get("at") or now) if isinstance(b["last"], dict) else now}
        if b["state"] in ("behind", "updating", "paused", "failed") and b["hub"]:
            held("behind", self.behind_s, {"severity": "warning", "title": "Station behind the hub",
                                           "text": f"{b['text']} (the hub runs {short(b['hub'])})"})
        for d in m["dongles"]:
            if not d["present"]:
                held(f"sdr:{d['serial']}", 0, {"severity": "critical", "title": f"SDR {d['name']} gone",
                                               "text": f"dongle {d['name']} ({d['serial']}) is unplugged or not found"
                                                       + (f": its job {d['job']} isn't running" if d["job"] != "Off" else "")})
            elif d["job"] != "Off" and not d["running"] and (d["job"] != P25_JOB or m["processes"]["recorder"] is None):
                held(f"job:{d['job']}", JOB_GRACE_S, {"severity": "critical", "title": f"{d['job']} not running",
                                                     "text": f"{d['job']} on SDR {d['name']}: {d['state'] or '?'}"
                                                             + (f" ({d['detail']})" if d["detail"] else "")
                                                             + (f", {d['restarts']} restarts" if d["restarts"] else "")})
        if not m["shared"]:                              # one dongle per channel: each channel is a job
            hl_jobs = ((self.hub.status or {}).get("health") or {}).get("jobs") or {}
            for c in m["channels"]:
                j = hl_jobs.get(c["label"]) if isinstance(hl_jobs, dict) else None
                state = (j or {}).get("state", c["state"])
                if state in ("down", "absent", "starting") and (j or {}).get("expected", True) and c["mhz"]:
                    held(f"job:{c['label']}", JOB_GRACE_S, {"severity": "critical", "title": f"{c['label']} not running",
                                                           "text": f"{c['label']}: {state}"
                                                                   + (f" ({c['detail']})" if c["detail"] else "")})
        wh = m["processes"]["whisper"]
        if wh and wh.get("state") not in ("ok", "off", None):
            held("whisper", self.down_s, {"severity": "warning", "title": "Speech-to-text down",
                                          "text": f"whisper-server: {wh.get('state')}"
                                                  + (f" ({wh['detail']})" if wh.get("detail") else "")
                                                  + ": transmissions come up without text"})
        sq = m.get("stt_queue")
        if sq and ((sq.get("oldest_s") or 0) >= STT_BACKLOG_S):
            out["stt_backlog"] = {"severity": "warning", "title": "Speech-to-text behind",
                                  "text": f"the station's whisper is behind: its oldest waiting clip is from "
                                          f"{_hhmm(now - float(sq.get('oldest_s') or 0))} (pages, Fire/EMS 15 and the "
                                          "firegrounds go first; clips waiting too long are skipped and say so)",
                                  "since": now - float(sq.get("oldest_s") or 0)}
        rec = m["processes"]["recorder"]
        if rec and rec.get("expected") and rec.get("state") != "ok":
            held("recorder", self.down_s, {"severity": "critical", "title": "trunk-recorder down",
                                           "text": f"trunk-recorder: {rec.get('state')}"
                                                   + (f" ({rec['detail']})" if rec.get("detail") else "")
                                                   + f": {P25_JOB} (P25) isn't being heard"})
        for c in m["channels"]:
            if c["stuck"]:
                out[f"stuck:{c['label']}"] = {
                    "severity": "warning", "title": f"{c['label']} stuck open",
                    "text": f"{c['label']} squelch open without a break since {_hhmm(now - float(c['open_s'] or 0))}: "
                            "likely noise holding it (check the squelch, the antenna)",
                    "since": now - float(c["open_s"] or 0)}      # (no running duration in the text: the banner counts)
        ob = m["outbox"]
        if ob["pending"] and ob["age_s"] is not None and ob["age_s"] >= self.outbox_s:
            out["outbox"] = {"severity": "warning", "title": "Station's events backing up",
                             "text": f"{ob['pending']} event(s) waiting on the station, the oldest from {_hhmm(ob['oldest'])}: "
                                     "it couldn't reach the hub (or the hub couldn't take them)",
                             "since": ob["oldest"]}
        disk = m["system"].get("disk_pct")
        if isinstance(disk, (int, float)) and disk >= self.disk_pct:
            out["disk"] = {"severity": "critical" if disk >= DISK_CRITICAL else "warning", "title": "Station disk full",
                           "text": f"the station's disk is {disk:.0f}% full" + (
                               f" ({m['system']['disk_free'] / 1e9:.1f} GB free)" if m["system"].get("disk_free") else ""),
                           "since": now}
        for k in [k for k in self._first if k not in seen]:
            del self._first[k]                          # not holding any more: its clock starts again
        for k, a in out.items():
            if a is not None:
                a["who"] = self._who(k, m)
        return out, True

    def _who(self, key: str, m: dict) -> dict:
        """Who / where an alert is about (sizeup/notify.py Who): the station, the dongle, the channel."""
        w: dict = {"station": m["station"]["name"] or m["station"]["id"] or "the station"}
        kind, _, arg = key.partition(":")
        if kind == "silent":
            last = self.hub.last_state
            w["detail"] = f"last heard {_hhmm(last)}" if last else "never reported"
        elif kind == "sdr":
            d = next((d for d in m["dongles"] if d["serial"] == arg), None)
            w["dongle"] = f"SDR {d['name'] if d else arg}"
            if d and d["job"] != "Off":
                w["channel"] = d["job"]
        elif kind == "job":
            w["channel"] = arg
            d = next((d for d in m["dongles"] if d["job"] == arg), None)
            if d:
                w["dongle"] = f"SDR {d['name']}"
        elif kind == "stuck":
            w["channel"] = arg
        elif kind == "recorder":
            w["channel"] = P25_JOB
        elif kind in ("update", "behind"):
            w["detail"] = f"station {short(m['build']['station'])}, hub {short(m['build']['hub'])}"
        elif kind == "outbox":
            w["detail"] = f"{m['outbox']['pending']} event(s) waiting"
        elif kind == "disk" and isinstance(m["system"].get("disk_pct"), (int, float)):
            w["detail"] = f"disk {m['system']['disk_pct']:.0f}% full"
        elif kind == "transcribe":
            w["detail"] = "Amazon Transcribe" + (f": {(m.get('transcribe') or {}).get('reason_text')}"
                                                 if (m.get("transcribe") or {}).get("reason_text") else "")
        return w

    # -- the loop ---------------------------------------------------------------------------------------------------

    def tick(self, now: float | None = None) -> list[tuple[str, dict]]:
        """Check everything once: [("start" | "clear", alert)] for what changed."""
        now = self.clock() if now is None else now
        with self._lock:
            m = self.model(now)
            conds, judged = self.conditions(m, now)
            changes: list[tuple[str, dict]] = []
            for key, c in conds.items():
                if c is None:
                    continue
                a = self.active.get(key)
                if a is None:
                    a = {"key": key, "severity": c["severity"], "title": c["title"], "text": c["text"],
                         "since": float(c.get("since") or now), "raised": now, "pushed": False, "who": c.get("who") or {}}
                    self.active[key] = a
                    changes.append(("start", a))
                    self.log(f"health: ALERT {a['title']}: {a['text']} (since {_hhmm(a['since'])})")
                else:
                    a.update(severity=c["severity"], title=c["title"], text=c["text"], who=c.get("who") or a.get("who") or {})
            for key in list(self.active):
                if key in conds:
                    continue
                if key not in ("silent", "transcribe", "aws_budget80", "aws_budget") and not judged:
                    continue                             # the station's data is old: leave it as it was
                a = self.active.pop(key)
                a["cleared"] = now
                self.history.append(a)
                changes.append(("clear", a))
                self.log(f"health: cleared {a['title']} (after {_dur(now - a['since'])})")
            cut = now - HISTORY_S
            self.history = [h for h in self.history if (h.get("cleared") or now) >= cut][-HISTORY_MAX:]
            for kind, a in changes:
                if kind == "clear" and a.get("pushed"):
                    self._push(a, now, recovered=True)
            for a in self.active.values():
                if not a.get("pushed") and now - self.last_push.get(a["key"], 0.0) >= self.repeat_s:
                    self._push(a, now)
            if changes:
                self._save()
            self._save_gaps(now)
        return changes

    def _push(self, a: dict, now: float, recovered: bool = False) -> None:
        n = self.notifier
        if n is None or not n.enabled:
            return
        click = f"{self.public_url}/cad#settings/system/health" if self.public_url else ""
        who = a.get("who") or None
        if recovered:
            n.send(f"Recovered: {a['title']}", f"{a['title']} cleared at {_hhmm(a.get('cleared'))} after "
                                               f"{_dur((a.get('cleared') or now) - a['since'])}.",
                   priority=3, tags=("white_check_mark", "sizeup"), click=click, who=who)
            return
        a["pushed"] = True
        self.last_push[a["key"]] = now
        long = now - a["since"]
        n.send(a["title"] + (f" {_dur(long)}" if long >= 60 else ""), f"{a['text']} (since {_hhmm(a['since'])}).",
               priority=4 if a["severity"] == "critical" else 3,
               tags=("rotating_light" if a["severity"] == "critical" else "warning", "sizeup"), click=click, who=who)
        self._save()

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # the watcher must never die
                self.log(f"health: {type(e).__name__}: {e}")

    # -- what people and CI read ------------------------------------------------------------------------------------

    def alerts(self) -> list[dict]:
        """The active alerts, worst first then oldest: the banner (the CAD state's cloud.alerts)."""
        with self._lock:
            out = [dict({k: a[k] for k in ("key", "severity", "title", "text", "since")}, who=a.get("who") or {})
                   for a in self.active.values()]
        return sorted(out, key=lambda a: (a["severity"] != "critical", a["since"]))

    def panel(self) -> dict:
        now = self.clock()
        with self._lock:
            hist = sorted(self.history, key=lambda h: -(h.get("cleared") or 0))
            hist = [h for h in hist if (h.get("cleared") or now) >= now - HISTORY_S]
            pushed = dict(self.last_push)
        n = self.notifier
        return {"health": self.model(now), "alerts": self.alerts(),
                "history": [{k: h.get(k) for k in ("key", "severity", "title", "text", "since", "cleared", "pushed", "who")}
                            for h in hist],
                "notify": {"on": bool(n is not None and n.enabled),
                           "server": getattr(n, "server", "") if n is not None and n.enabled else "",
                           "sent": getattr(n, "sent", 0), "failed": getattr(n, "failed", 0),
                           "last_error": getattr(n, "last_error", ""), "last_push": max(pushed.values(), default=None)},
                "thresholds": {"silent_min": self.silent_s / 60, "behind_min": self.behind_s / 60,
                               "down_min": self.down_s / 60, "outbox_min": self.outbox_s / 60,
                               "disk_pct": self.disk_pct, "repeat_min": self.repeat_s / 60,
                               "job_grace_s": JOB_GRACE_S}}

    def status(self) -> dict:
        """GET /api/station/status: what the deploy check needs, nothing else (no names of people, no audio)."""
        now = self.clock()
        m = self.model(now)
        b = m["build"]
        last = b["last"] or {}
        return {"ok": True, "at": now, "hub_build": self.hub.build,
                "station": {"reported": m["reported"], "connected": m["station"]["connected"],
                            "last_state": m["station"]["last_state"], "silent": m["station"]["silent"],
                            "build": b["station"], "state": b["state"], "text": b["text"],
                            "update": b["update"],
                            "last": {k: last.get(k) for k in ("at", "from", "to", "result", "why", "seconds", "staged_s",
                                                              "waited_s", "up_s", "radios_s", "line")} if last else None},
                "alerts": self.alerts()}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] != ["status-token"]:
        print("usage: SIZEUP_STATION_TOKEN=... python -m sizeup.cloud.health status-token", file=sys.stderr)
        return 2
    tok = os.environ.get("SIZEUP_STATION_TOKEN", "").strip()
    if not tok:
        print("set SIZEUP_STATION_TOKEN (the hub's station token) first", file=sys.stderr)
        return 2
    print(status_token(tok))
    return 0


if __name__ == "__main__":
    sys.exit(main())
