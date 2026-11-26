"""The log sentinel: a test fails when a known-sensitive string reaches a log line.

The hub's log goes to CloudWatch (its stdout to /ecs/sizeup-hub, the station's journal to /sizeup/station), read by
whoever can read the account's logs. It must never carry transcript text, rule or phrase text, place or town names,
addresses, names or plates: ids, counts and exception TYPE names only (the words stay in the database, behind sign-in).

    def test_x(log_sentinel):                       # the fixture (tests/conftest.py makes it available everywhere)
        thing = Thing(log=log_sentinel.log)         # the app's own log() (sizeup/app.py): a line on stdout
        ...                                         # anything else the code prints, logs or raises on a thread
        assert_no_sensitive(log_sentinel, ["ZZQ-SENTINEL-PHRASE", "Example Town"])

What it captures, every path a line can take out of the process:
    stdout and stderr at the file descriptor (print, sys.stdout.write, app.log, a child process's output)
    the logging module, every logger at DEBUG (with each record's traceback text)
    an exception that escapes a thread (threading.excepthook: the default writes its traceback, message included, to
    stderr; pytest's own hook would turn it into a warning instead, so it is put back to what the app has live)
    an exception that can't be raised (sys.unraisablehook: a __del__, a generator's close)

The strings are matched without regard to case or runs of whitespace. A sentinel is INVENTED (this repository is
public): never a real phrase, place, name or plate.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import traceback

import pytest


class Captured:
    """What reached any log path since the fixture started (lines() reads what is new and keeps it)."""

    def __init__(self, capfd, caplog):
        self._capfd, self._caplog = capfd, caplog
        self._seen: list[str] = []

    @staticmethod
    def log(msg: str) -> None:
        """The app's own log() (sizeup/app.py: one write to stdout), passed as a component's log= in a test."""
        from sizeup.app import log
        log(msg)

    def lines(self) -> list[str]:
        out, err = self._capfd.readouterr()
        self._seen += out.splitlines() + err.splitlines()
        for r in self._caplog.records:
            self._seen += [f"{r.levelname}:{r.name}:{r.getMessage()}", *(r.exc_text or "").splitlines()]
            if r.exc_info and r.exc_info[1] is not None:
                self._seen += "".join(traceback.format_exception(*r.exc_info)).splitlines()
        self._caplog.clear()
        return list(self._seen)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower()


def assert_no_sensitive(captured, sensitive) -> list[str]:
    """Fails naming the sentinel and the line that carried it. captured: the fixture, or the lines themselves (a list
    of strings: a component's own log= list). The lines, for further checks."""
    lines = captured.lines() if isinstance(captured, Captured) else [str(x) for x in captured]
    words = [_norm(s) for s in sensitive if _norm(s)]
    assert words, "no sentinel strings given"
    for line in lines:
        low = _norm(line)
        for s in words:
            assert s not in low, f"a sensitive string ({s!r}) reached a log line: {line[:300]!r}"
    return lines


@pytest.fixture
def log_sentinel(capfd, caplog, monkeypatch):
    """Every log path captured (see the module's doc); assert_no_sensitive(log_sentinel, [...]) checks them."""
    caplog.set_level(logging.DEBUG)

    def thread_hook(args):                 # as the default: the traceback, its message included, on stderr
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", "?")
        sys.stderr.write(f"Exception in thread {name}:\n")
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=sys.stderr)

    def unraisable_hook(u):
        sys.stderr.write(f"{u.err_msg or 'Exception ignored in'}: {u.object!r}\n")
        if u.exc_type is not None:
            traceback.print_exception(u.exc_type, u.exc_value, u.exc_traceback, file=sys.stderr)

    monkeypatch.setattr(threading, "excepthook", thread_hook)
    monkeypatch.setattr(sys, "unraisablehook", unraisable_hook)
    return Captured(capfd, caplog)
