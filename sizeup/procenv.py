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


def register_secrets() -> None:
    """Every credential's name and value (and the station's own secrets in the environment) into the log scrub
    (sizeup/logscrub.py): a station log line with one of them never leaves the station."""
    from sizeup import logscrub
    for k, v in os.environ.items():
        if SECRET_ENV.search(k):
            logscrub.secret(v)
    d = _dir
    if d is None:
        return
    try:
        files = [f for f in d.iterdir() if f.is_file()]
    except OSError:
        return
    for f in files:
        logscrub.secret_name(f.name.removeprefix("sizeup-adapter."))
        try:
            logscrub.secret(f.read_text().rstrip("\n"), short=True)
        except OSError:
            pass


def credential(name: str) -> str:
    """An adapter credential's value ("" when it isn't there): read from the service's credentials directory."""
    if _dir is None or not name or "/" in name or name.startswith("."):
        return ""
    for n in (f"sizeup-adapter.{name}", name):          # (ImportCredential= / LoadCredential= name it so)
        try:
            v = (_dir / n).read_text().rstrip("\n")
        except OSError:
            continue
        from sizeup import logscrub
        logscrub.secret(v, short=True)                    # never in a station log line that leaves (logship.py)
        logscrub.secret_name(name)
        return v
    return ""


def child_env(base=None, **extra) -> dict:
    """os.environ (or base) for a program sizeup starts, without the credentials directory or any adapter credential."""
    env = dict(os.environ if base is None else base)
    for k in (*STRIP, *_names):
        env.pop(k, None)
    env.update(extra)
    return env
