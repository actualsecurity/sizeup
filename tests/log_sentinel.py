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

The static half, exception_text_in_logs(paths): an ast walk that finds a log call carrying an exception's text (see
its doc), for a module's tests to assert it finds nothing.
"""

from __future__ import annotations

import ast
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


# -- the static half: an exception's text in a log call -------------------------------------------------------------

LOG_NAMES = ("log", "print", "warn", "warning", "error", "info", "debug", "exception", "critical", "write")
SAFE_CALLS = frozenset({"type",        # type(e), type(e).__name__
                        "failure"})    # notify.failure(e): the HTTP status or the error's kind, never its text
SAFE_ATTRS = frozenset({"status", "code"})    # e.status, e.code, getattr(e, "code", ...): a status or an error code


def _dotted(node) -> str:
    """"self.last_error" for an Attribute chain on a Name, "msg" for a Name, else ""."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return ""


def _call_name(call: ast.Call) -> str:
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _uses(node, tainted: set[str]) -> bool:
    """node reads a tainted name (an except's variable, or what was made from one), outside type(...), a SAFE_CALLS
    call and a SAFE_ATTRS read of it."""
    if isinstance(node, ast.Call):
        fn = _call_name(node)
        if isinstance(node.func, ast.Name) and fn in SAFE_CALLS:
            return False
        if (fn == "getattr" and isinstance(node.func, ast.Name) and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in SAFE_ATTRS):
            return any(_uses(a, tainted) for a in node.args[2:])
    if isinstance(node, ast.Attribute):
        if node.attr in SAFE_ATTRS and isinstance(node.value, ast.Name) and node.value.id in tainted:
            return False
        d = _dotted(node)
        if d and d in tainted:
            return True
    if isinstance(node, ast.Name):
        return node.id in tainted
    return any(_uses(c, tainted) for c in ast.iter_child_nodes(node))


def _targets(node) -> list[str]:
    ts = node.targets if isinstance(node, ast.Assign) else [node.target]
    out = []
    for t in ts:
        for x in (t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]):
            if d := _dotted(x):
                out.append(d)
    return out


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own(scope):
    """The scope's nodes, never a nested function's (it is a scope of its own: its names are its own)."""
    todo = list(ast.iter_child_nodes(scope))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, _FUNCS):
            todo.extend(ast.iter_child_nodes(n))


def _scope_findings(scope, where: str) -> list[str]:
    """One function (or the module): every handler's variable is tainted inside its handler; a name or attribute
    assigned from a tainted value there is tainted in the whole scope (msg = f"{e}" ... log(msg) after the loop)."""
    out = []
    derived: set[str] = set()
    own = list(_own(scope))
    handlers = [h for h in own if isinstance(h, ast.ExceptHandler) and h.name]
    changed = True
    while changed:                                     # to a fixpoint: msg2 = msg + "x"
        changed = False
        for h in handlers:
            for n in (x for b in h.body for x in [b, *_own(b)]):
                if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and n.value is not None \
                        and _uses(n.value, {h.name} | derived):
                    new = set(_targets(n)) - derived - {h.name}
                    if new:
                        derived |= new
                        changed = True
        if derived:                                    # and outside the handlers, from a derived one
            for n in own:
                if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and n.value is not None \
                        and _uses(n.value, derived):
                    new = set(_targets(n)) - derived
                    if new:
                        derived |= new
                        changed = True
    inside: dict[int, set[str]] = {}
    for h in handlers:
        for b in h.body:
            for n in [b, *_own(b)]:
                inside.setdefault(id(n), set()).add(h.name)
    for n in own:
        if not isinstance(n, ast.Call):
            continue
        fn = _call_name(n)
        line = f"{where}:{n.lineno}"
        if fn == "exception" and isinstance(n.func, ast.Attribute):
            out.append(f"{line}: .exception() logs the traceback")
            continue
        if any(k.arg == "exc_info" for k in n.keywords):
            out.append(f"{line}: exc_info= logs the traceback")
            continue
        if not fn or not fn.endswith(LOG_NAMES):
            continue
        tainted = derived | inside.get(id(n), set())
        if tainted and any(_uses(a, tainted) for a in [*n.args, *(k.value for k in n.keywords)]):
            out.append(f"{line}: {fn}() carries an exception's text")
    return out


def exception_text_in_logs(paths) -> list[str]:
    """"file:line: why" for every log call (a function or method whose name ends in log, print, warn, warning, error,
    info, debug, exception, critical or write) whose arguments (f-string parts, %-format and .format arguments,
    keywords) read an exception: the variable of an enclosing `except ... as name`, or a name or attribute assigned
    from it in the same function. type(name) / type(name).__name__, name.status / name.code and SAFE_CALLS are allowed.
    Any .exception() call and any exc_info= keyword are flagged outright. paths: files, or (name, source) pairs."""
    out = []
    for p in paths:
        name, src = (p if isinstance(p, tuple) else (getattr(p, "name", str(p)), open(p, encoding="utf-8").read()))
        tree = ast.parse(src)
        scopes = [tree, *(n for n in ast.walk(tree) if isinstance(n, _FUNCS))]
        for sc in scopes:
            out += _scope_findings(sc, name)
    return sorted(set(out), key=lambda x: (x.split(":")[0], int(x.split(":")[1])))
