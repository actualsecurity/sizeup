"""Station forwarding on and off while the app runs: Settings > System > Station (user, 2026-10-08:
"how do we turn on and off station mode though? id like that to be easy to do from the ui").

    state/cloud.json   {"forward": bool, "hub_url": str, "at", "by"}: written by the first change
                       made there; from then on it wins over config.toml's [cloud] role / hub_url,
                       which stay the default until then
    On    a StationLink (station.py) over the one outbox: what is heard queues and goes up, the
          console's banner says the hub is the CAD of record, the monitor has its Hub row
    Off   all-in-one: nothing new is queued, the link's threads end, the banner and the Hub row go.
          What was already queued stays in state/outbox.sqlite and goes up first when it's back on.
    Radios, speech-to-text and the voice are never touched. The hub itself can't be switched here
    (a hub is a cloud deployment: the server answers 409).

Secrets, each on its own (never logged, never in an answer: only where it came from):
    the environment (SIZEUP_STATION_TOKEN, SIZEUP_ACCESS_CLIENT_ID, SIZEUP_ACCESS_CLIENT_SECRET),
    else [cloud] token / access_client_id / access_client_secret, else the macOS Keychain:
    `security find-generic-password -s <service> -w` with services sizeup-station-token,
    sizeup-access-id, sizeup-access-secret ([cloud] keychain_prefix / keychain_* rename them).
    Looked up when the link starts, on a change and on Test: not on every status poll.

Every change goes in the Settings audit (section "system") with the operator who made it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from sizeup.cloud.client import HubClient, HubError
from sizeup.cloud.outbox import Outbox
from sizeup.cloud.station import StationLink
from sizeup.config import hub_url_problem

SECURITY = "/usr/bin/security"
RUN = subprocess.run              # tests replace this: no test ever reads the real Keychain
# name -> (environment variable, Config field with the config file's value, Config field with the Keychain service)
SECRETS = {
    "token": ("SIZEUP_STATION_TOKEN", "cloud_token", "cloud_keychain_token"),
    "access_id": ("SIZEUP_ACCESS_CLIENT_ID", "cloud_access_id", "cloud_keychain_access_id"),
    "access_secret": ("SIZEUP_ACCESS_CLIENT_SECRET", "cloud_access_secret", "cloud_keychain_access_secret"),
}


def keychain_available() -> bool:
    return sys.platform == "darwin" and os.path.exists(SECURITY)


def keychain(service: str) -> str:
    """The generic password stored under service in the login Keychain, "" if none (or not macOS)."""
    if not service or not keychain_available():
        return ""
    try:
        r = RUN([SECURITY, "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _hhmm(t: float | None) -> str:
    return time.strftime("%H:%M", time.localtime(t)) if t else ""


class StationMode:
    def __init__(self, app, cfg, log: Callable[[str], None] = print,
                 client: Callable[[float], HubClient] | None = None):
        self.app, self.cfg, self.log = app, cfg, log
        self.path = Path(cfg.cloud_state) if cfg.cloud_state else None
        saved = self._load()
        self.source = "settings" if saved is not None else "config"
        self.forward = bool(saved.get("forward")) if saved is not None else cfg.cloud_role == "station"
        self.hub_url = str((saved or {}).get("hub_url") or cfg.cloud_hub_url or "").rstrip("/")
        self.link: StationLink | None = None
        self.problem = ""                       # on, but can't run: why (no token, no hub URL)
        self.started = False
        self._client = client                   # tests: the link's client factory
        self._outbox: Outbox | None = None
        self._values: dict[str, str] = {}       # the secrets themselves: only ever handed to the link
        self._where: dict[str, str] = {}        # name -> "env" | "config" | "Keychain" | ""
        self._lock = threading.RLock()
        app.station = self
        app.cad.cloud = self.cad_cloud
        if self.forward:
            self._up()                          # app.events set now: nothing heard from here on is missed

    # -- state/cloud.json ------------------------------------------------------------------------------

    def _load(self) -> dict | None:
        if self.path is None or not self.path.is_file():
            return None
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else None
        except (OSError, ValueError) as e:
            self.log(f"station: {self.path.name} unreadable ({e}): using config.toml")
            return None

    def _save(self, operator: str) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"forward": self.forward, "hub_url": self.hub_url, "at": time.time(), "by": operator},
                                  indent=1) + "\n")
        tmp.replace(self.path)

    # -- secrets ---------------------------------------------------------------------------------------

    def _resolve(self) -> None:
        values, where = {}, {}
        for name, (env, field_, service) in SECRETS.items():
            v, src = os.environ.get(env, "").strip(), "env"
            if not v:
                v, src = str(getattr(self.cfg, field_, "") or ""), "config"
            if not v:
                v, src = keychain(getattr(self.cfg, service, "")), "Keychain"
            values[name], where[name] = v, (src if v else "")
        self._values, self._where = values, where

    def secrets(self) -> dict:
        """What the page shows: where each secret came from, never the value."""
        out = {}
        for name, (env, _, service) in SECRETS.items():
            src = self._where.get(name, "")
            bad = name == "token" and src and len(self._values.get("token", "")) < 16
            out[name] = {"set": bool(src) and not bad, "source": src, "env": env,
                         "keychain": getattr(self.cfg, service, ""), "short": bool(bad)}
        return out

    def _missing(self) -> str:
        """Why the link can't run, "" when it can."""
        if hub_url_problem(self.hub_url):
            return "no hub URL" if not self.hub_url else f"hub URL: {hub_url_problem(self.hub_url)}"
        tok = self._values.get("token", "")
        if not tok:
            where = (f"Keychain {self.cfg.cloud_keychain_token} or " if keychain_available() else "")
            return f"no token ({where}SIZEUP_STATION_TOKEN)"
        if len(tok) < 16:
            return "token too short (at least 16 characters)"
        return ""

    # -- on / off -------------------------------------------------------------------------------------

    def outbox(self) -> Outbox | None:
        """The one outbox (opened when first needed, kept across on and off); None when there is none yet."""
        if self._outbox is None:
            path = self.cfg.cloud_outbox or (str(self.path.parent / "outbox.sqlite") if self.path else "")
            if not self.forward and (not path or not Path(path).is_file()):
                return None
            self._outbox = Outbox(path or ":memory:", int(self.cfg.cloud_outbox_max_mb * 1024 * 1024))
        return self._outbox

    def _up(self) -> None:
        """Forwarding on: a link over the outbox, its threads running once the app runs and the
        secrets are there; until then events still queue (nothing is lost) and the page says why."""
        self._resolve()
        self.problem = self._missing()
        v = self._values
        want = (self.hub_url, v.get("token", ""), v.get("access_id", ""), v.get("access_secret", ""))
        link = self.link
        if link is None or link.stopped or (link.cfg.cloud_hub_url, link.cfg.cloud_token, link.cfg.cloud_access_id,
                                            link.cfg.cloud_access_secret) != want:
            if link is not None:
                link.stop()
            cfg = replace(self.cfg, cloud_hub_url=want[0], cloud_token=want[1], cloud_access_id=want[2],
                          cloud_access_secret=want[3])
            link = self.link = StationLink(self.app, cfg, log=self.log, client=self._client, outbox=self.outbox())
            self.app.cad.cloud = self.cad_cloud       # (StationLink set its own)
        if self.problem:
            self.log(f"station: forwarding is on but waiting: {self.problem}")
        elif self.started and not link.running:
            link.start()
            link.live_changed()

    def _down(self) -> None:
        if self.link is not None:
            self.link.stop()
        self.link, self.problem = None, ""
        self.app.events, self.app.link = None, None

    def start(self) -> None:
        """With the app (App.start): the link's threads, when forwarding is on and it can run."""
        with self._lock:
            self.started = True
            if self.link is not None and not self.problem:
                self.link.start()

    def stop(self) -> None:
        with self._lock:
            self.started = False
            if self.link is not None:
                self.link.stop()

    def set(self, body: dict) -> dict:
        """POST /api/station-mode {forward?, hub_url?, operator?}: ValueError with the reason if not."""
        forward, url = body.get("forward", self.forward), body.get("hub_url", self.hub_url)
        if not isinstance(forward, bool):
            raise ValueError("forward: true or false")
        if not isinstance(url, str) or len(url) > 300:
            raise ValueError("hub_url: the hub's URL, https://...")
        url = url.strip().rstrip("/")
        if (url or forward) and (why := hub_url_problem(url)):
            raise ValueError(f"hub URL: {why}")
        operator = " ".join(str(body.get("operator") or "").split())[:80]
        with self._lock:
            was = (self.forward, self.hub_url)
            self.forward, self.hub_url = forward, url
            if forward:
                self._up()
            else:
                self._down()
            self.source = "settings"
            self._save(operator)
            self._audit(operator, was)
        sync = getattr(self.app, "prealert_sync", None)
        if sync is not None:                    # the phone pre-alert follows: on here only while not forwarding
            sync()
        if was != (forward, url):
            self.log(f"station: forwarding {'on' if forward else 'off'}" + (f" to {url}" if forward else "")
                     + f" (Settings, {operator or 'no name'})")
        return self.state()

    def _audit(self, operator: str, was: tuple[bool, str]) -> None:
        changes = []
        if was[0] != self.forward:
            changes.append(("station forwarding", "on" if was[0] else "off", "on" if self.forward else "off"))
        if was[1] != self.hub_url:
            changes.append(("station hub URL", was[1], self.hub_url))
        cad = getattr(self.app, "cad", None)
        if not changes or cad is None:
            return
        from sizeup.cad.settings import Editor
        try:
            with cad.store.tx() as db:
                for key, old, new in changes:
                    Editor._audit(db, time.time(), operator, "system", key, old, new)
            cad.store.rev += 1                  # the Settings page (and its audit log) reloads
        except Exception as e:  # noqa: BLE001 - the switch itself already happened
            self.log(f"station: audit not written: {type(e).__name__}: {e}")

    # -- what the pages show --------------------------------------------------------------------------

    def status(self) -> dict:
        """{state, text, detail, waiting, ...}: the card's status line."""
        ob = self._outbox if self._outbox is not None else (self.outbox() if not self.forward else None)
        n, oldest = ob.pending() if ob is not None else (0, None)
        base = {"waiting": n, "oldest": oldest, "last_error": "", "error_since": None, "last_ok": None}
        if not self.forward:
            return dict(base, state="off", text="Off" + (f" · {n} waiting" if n else ""),
                        detail="All-in-one: nothing goes to the hub" + (f"; {n} event(s) from before wait to go up "
                                                                        "when it's turned on" if n else ""))
        link = self.link
        if self.problem or link is None:
            return dict(base, state="blocked", text=f"Not sending: {self.problem or 'starting'}" + (f" · {n} waiting" if n else ""),
                        detail=f"Forwarding is on, but {self.problem or 'it is starting'}. Events queue here meanwhile.")
        row = link.status_row()
        out = dict(base, last_error=link.last_error, error_since=link.error_since, last_ok=link.last_ok, detail=row["detail"])
        if not self.started:
            return dict(out, state="starting", text="Starting")
        if row["state"] == "ok":
            return dict(out, state="ok", text="Connected" + (f" · {n} waiting" if n else ""))
        if row["state"] == "starting":
            return dict(out, state="starting", text="Connecting" + (f" · {n} waiting" if n else ""))
        return dict(out, state="down", text=f"Offline since {_hhmm(link.error_since or link.last_ok)}"
                    + (f" · {n} waiting" if n else ""))

    def state(self) -> dict:
        """GET /api/station-mode."""
        with self._lock:
            if not self._where:
                self._resolve()                 # once; again on a change and on Test
            return {"forward": self.forward, "hub_url": self.hub_url, "from": self.source,
                    "config_role": self.cfg.cloud_role or "all-in-one",
                    "running": bool(self.link is not None and self.link.running),
                    "status": self.status(), "secrets": self.secrets(), "keychain": keychain_available()}

    def hub_row(self) -> dict | None:
        """The monitor header's Hub row: only while forwarding is on."""
        if not self.forward:
            return None
        link = self.link
        if link is not None and not self.problem:
            return link.status_row()
        return {"label": "Hub", "mhz": None, "state": "down", "word": "not sending",
                "detail": f"forwarding to the hub is on, but {self.problem or 'starting'}: Settings > System > Station",
                "open": False, "quieting_db": None, "restarts": 0, "last_heard": None}

    def cad_cloud(self) -> dict | None:
        """The console's header pill and banner: None all-in-one (forwarding off)."""
        if not self.forward:
            return None
        link = self.link
        if link is not None and not self.problem:
            return link.cad_cloud()
        row = self.hub_row() or {}
        return {"role": "station", "hub_url": self.hub_url, "state": "down", "blocked": True, "detail": row.get("detail", "")}

    # -- Test connection ------------------------------------------------------------------------------

    def test(self, body: dict) -> dict:
        """POST /api/station-mode/test {hub_url?}: GET /healthz on the hub, then the station path
        (/api/station/hello) with the bearer token, both with the Access service-token headers.
        {ok, status, text}: ok = the station path took the token (a /healthz that only people's Access
        lets through is said, not a failure). A fresh look at the secrets: on, but waiting for one:
        it starts now."""
        url = body.get("hub_url") or self.hub_url
        if not isinstance(url, str) or (why := hub_url_problem(url.strip().rstrip("/"))):
            return {"ok": False, "status": 0, "text": f"Hub URL: {why if isinstance(url, str) else 'a string'}"}
        url = url.strip().rstrip("/")
        with self._lock:
            self._resolve()
            if self.forward and self.problem:
                self._up()
            v = dict(self._values)
        if not v.get("token"):
            return {"ok": False, "status": 0, "text": "No token: " + self._missing().removeprefix("no token ")}
        c = HubClient(url, v["token"], v.get("access_id", ""), v.get("access_secret", ""), timeout=10.0)
        try:
            try:
                h, health_err = c.request("GET", "/healthz", timeout=10.0), None
            except HubError as e:
                h, health_err = {}, e
            if health_err is not None and health_err.status == 0:
                return {"ok": False, "status": 0, "text": f"Can't reach it: {health_err}".replace("can't reach the hub: ", "")}
            is_hub = h.get("role") == "hub"
            if h and not is_hub:
                return {"ok": False, "status": 200, "text": f"That's not a hub: it says {str(h.get('role') or '?')[:40]}"}
            try:
                c.request("GET", "/api/station/hello", timeout=10.0)
            except HubError as e:
                if e.status == 401:
                    return {"ok": False, "status": 401, "text": "The hub refused the token (not the same as the hub's)"}
                if e.status == 403:
                    return {"ok": False, "status": 403, "text": "Refused: the hub has no station token, "
                                                               "or Cloudflare Access wants the service token"}
                if e.status in (301, 302, 303, 307):
                    return {"ok": False, "status": e.status, "text": "Cloudflare Access sent its login page: "
                                                                     "check the Access id and secret"}
                if not (e.status == 404 and is_hub):     # 404 from a hub from before /hello: it took the token
                    return {"ok": False, "status": e.status, "text": f"Station path: {e}"}
            ver = str(h.get("version") or "")[:40]
            if is_hub:
                return {"ok": True, "status": 200, "text": "OK: hub reached, token accepted" + (f" (hub {ver})" if ver else "")}
            # /healthz behind the people's Access app (the service token only opens /api/station/): fine
            return {"ok": True, "status": 200, "text": "OK: token accepted (the hub's /healthz didn't answer "
                                                       f"the station: {health_err})"}
        finally:
            c.close()
