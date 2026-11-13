"""The environment the station's programs get (#301): never the incident-source adapter's credentials.

The adapter's credentials reach the station service only as files under $CREDENTIALS_DIRECTORY (systemd
LoadCredential=, deploy/station/install-linux.sh / sizeup-station adapter setup), never as environment variables.
take_credentials() runs once at the station's start: it keeps that directory's path here and takes it (and any adapter
credential name, should one still be set) out of os.environ, so nothing started later inherits them. child_env() is the
explicit env= for every program sizeup starts (rtl_fm, whisper-server, trunk-recorder, Kokoro): the same, stripped
again. credential(name) is how an adapter reads one (sizeup.cloud.adapters re-exports it).

(The hub token, SIZEUP_STATION_TOKEN, is unchanged: still from station.env, so in the environment; see PR #305.)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_dir: Path | None = None
_names: set[str] = set()
STRIP = ("CREDENTIALS_DIRECTORY",)
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASS|CREDENTIAL|ACCESS_CLIENT|_KEY$|API_KEY")


def take_credentials(names=(), environ=None) -> Path | None:
    """At the station's start: $CREDENTIALS_DIRECTORY kept here and out of the environment, with any adapter
    credential name. The directory (None: no credentials loaded)."""
    global _dir
    env = os.environ if environ is None else environ
    d = env.pop("CREDENTIALS_DIRECTORY", "")
    if d:
        _dir = Path(d)
    _names.update(str(n) for n in names or ())
    for n in _names:
        env.pop(n, None)
    register_secrets()
    return _dir


# Every credstore value is a secret but non-secret configuration (a user name, a region, a URL or host, a bucket)
NOT_SECRET = re.compile(r"(?i)user|login|account|region|url|host|endpoint|bucket")
USER_NAME = re.compile(r"(?i)^(.*?)[_-]?(?:user(?:name)?|login|account)$")
_hosts: set[str] = set()


def _credstore() -> dict[str, str]:
    """{name: value} of the credentials directory (the "sizeup-adapter." prefix off the name); {} without one."""
    d = _dir
    out: dict[str, str] = {}
    if d is None:
        return out
    try:
        files = [f for f in d.iterdir() if f.is_file()]
    except OSError:
        return out
    for f in files:
        try:
            out[f.name.removeprefix("sizeup-adapter.")] = f.read_text().rstrip("\n")
        except OSError:
            pass
    return out


def _host_of(v: str) -> str:
    """A URL's or a host name's host ("" when v is neither)."""
    from urllib.parse import urlsplit
    v = (v or "").strip()
    h = urlsplit(v).hostname if "://" in v else (v if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?", v) else "")
    return (h or "").split(":")[0].lower()


def _register(name: str, value: str, store: dict[str, str]) -> None:
    from sizeup import logscrub
    logscrub.secret_name(name)
    if not NOT_SECRET.search(name):
        logscrub.secret(value, short=True)
        m = re.match(r"(?i)^(.*?)[_-]?(?:pass(?:word|wd)?|pwd|pw|secret|phrase)$", name)
        if m is not None:                       # its login's user, for the base64 of user:password
            for u in (f"{m.group(1)}_USER", f"{m.group(1)}_USERNAME", f"{m.group(1)}_LOGIN", f"{m.group(1)}USER"):
                if store.get(u):
                    logscrub.secret_pair(store[u], value)
    elif (h := _host_of(value)):
        _hosts.add(h)                           # the adapter's host: its lines stay on the station (logship.py)


def register_secrets() -> None:
    """The station's secrets into the log scrub (sizeup/logscrub.py): a station log line with one of them never leaves
    the station. Every credstore value but non-secret configuration (NOT_SECRET: a user name, a region, a URL or host,
    a bucket) is a secret; a user name or a region is not
    (it would drop every line that says it), but its name is redacted and a login's base64(user:password) is
    registered. A credential that is a URL or a host name is kept as one of the adapter's hosts (adapter_hosts())."""
    from sizeup import logscrub
    for k, v in os.environ.items():
        if SECRET_ENV.search(k):
            logscrub.secret(v)
    store = _credstore()
    for n, v in store.items():
        _register(n, v, store)


def adapter_hosts() -> set[str]:
    """The hosts in the adapter's non-secret credentials (its base URL, its host), read at runtime."""
    return set(_hosts)


def credential(name: str) -> str:
    """An adapter credential's value ("" when it isn't there): read from the service's credentials directory."""
    if _dir is None or not name or "/" in name or name.startswith("."):
        return ""
    for n in (f"sizeup-adapter.{name}", name):          # (ImportCredential= / LoadCredential= name it so)
        try:
            v = (_dir / n).read_text().rstrip("\n")
        except OSError:
            continue
        _register(name, v, _credstore())                  # never in a station log line that leaves (logship.py)
        return v
    return ""


def child_env(base=None, **extra) -> dict:
    """os.environ (or base) for a program sizeup starts, without the credentials directory or any adapter credential."""
    env = dict(os.environ if base is None else base)
    for k in (*STRIP, *_names):
        env.pop(k, None)
    env.update(extra)
    return env
