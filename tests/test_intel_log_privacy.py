"""The radio pre-alerts' log lines carry no words (sizeup/intel): an admin's rule phrase, a town in a pack's settings, a
transcript through feed() with locate, the model or the Listen link failing, reach the hub's log (CloudWatch) as ids,
counts and exception TYPE names only. The shared log sentinel: tests/log_sentinel.py.

Synthetic only (this repository is public): the sentinel strings are invented."""

from __future__ import annotations

import threading

import pytest

from sizeup.intel import listen as L
from sizeup.intel import rules as rules_mod
from sizeup.intel.engine import IntelEngine, LiveSink
from sizeup.intel.events import MemoryEvents
from tests.log_sentinel import assert_no_sensitive, exception_text_in_logs
from tests.test_intel import Clock, row

PHRASE = "ZZQ-SENTINEL-PHRASE"         # an admin's rule phrase
TOWN = "Example Town"                  # a town in a pack's settings (not a town of the list: refused)
SAID = "Zzqsentinelword"               # a word of a transcript
PLACE = "Zzqsentinel Plaza"            # a place in it
SENSITIVE = [PHRASE, TOWN, SAID, PLACE]


def boom(*_a, **_k):
    raise RuntimeError(f"failed on {SAID!r} at {PLACE} ({PHRASE}, {TOWN})")


@pytest.mark.parametrize("phrase", [f"re:{PHRASE}((", f"({PHRASE}", f"{PHRASE} ...", f"re:{PHRASE}" + "x" * 300],
                         ids=["bad_regex", "unbalanced", "trailing_gap", "too_long"])
def test_an_admins_bad_rule_phrase_is_logged_by_its_id_only(log_sentinel, phrase):
    settings = {"intel_rules": {"local.zzq": {"pack": "police", "category": "weapons", "any": [phrase]}},
                "intel_packs": {"police": {"threshold": 0.7}}}
    e = IntelEngine(settings=settings, events=MemoryEvents(), thread=False, clock=Clock(), log=log_sentinel.log)
    assert e.pack("police").threshold == 0.7                     # the good override stays
    assert not any(r.id == "local.zzq" for r in e.pack("police").rules)
    lines = assert_no_sensitive(log_sentinel, SENSITIVE)
    assert any("intel_rules/local.zzq not applied (" in x and "Error)" in x for x in lines), lines


def test_a_town_in_a_packs_settings_is_never_logged(log_sentinel):
    settings = {"intel_packs": {"ems": {"area": [TOWN]}}}
    e = IntelEngine(settings=settings, events=MemoryEvents(), thread=False, clock=Clock(), log=log_sentinel.log)
    assert e.pack("ems").area == rules_mod.load_packs()[2].area   # the file's area
    lines = assert_no_sensitive(log_sentinel, SENSITIVE)
    assert any("intel_packs/ems not applied (ValueError)" in x for x in lines), lines


def test_when_the_rest_still_wont_compile_neither_error_is_quoted(log_sentinel, monkeypatch):
    real = rules_mod.load_packs

    def load(srcs=None, ov=None):
        if ov:
            raise ValueError(f"rule {PHRASE!r} in {TOWN}")
        return real(srcs, ov)
    monkeypatch.setattr(rules_mod, "load_packs", load)
    e = IntelEngine(settings={"intel_packs": {"ems": {"area": [TOWN]}}}, events=MemoryEvents(), thread=False,
                    clock=Clock(), log=log_sentinel.log)
    assert [p.name for p in e.packs] == [p.name for p in real()]  # the pack files alone
    lines = assert_no_sensitive(log_sentinel, SENSITIVE)
    assert any("rule changes not applied (ValueError; ValueError)" in x for x in lines), lines


def test_a_settings_key_that_is_no_id_is_never_logged(log_sentinel):
    settings = {"intel_rules": {f"local.{PHRASE} at {TOWN}": {"pack": "police", "category": "weapons", "any": ["(x"]}}}
    IntelEngine(settings=settings, events=MemoryEvents(), thread=False, clock=Clock(), log=log_sentinel.log)
    lines = assert_no_sensitive(log_sentinel, SENSITIVE)
    assert any("(a key that is no id) not applied" in x for x in lines), lines


class Notifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority, tags, click, **kw):
        self.sent.append((title, body))
        return True


class Infer:
    """The model (infer.Inference) failing on every call, its exception quoting the words."""
    asked: dict = {}

    def __getattr__(self, name):
        return boom


class Backing:
    """The local model behind the police rules (backing.ModelBacking), failing with the words in its exception."""

    def owns(self, pack):
        return False

    consider = staticmethod(boom)


class Bucket:
    def __init__(self):
        self.puts = []

    def put(self, key, body, ctype):
        self.puts.append(key)

    def presign(self, key, expires_s, ctype):
        return f"https://bucket.example/{key}"


def live_engine(log, locate=boom):
    e = IntelEngine(settings={}, events=MemoryEvents(), locate=locate, thread=False, clock=Clock(), log=log)
    e.sink, e.notifier = LiveSink(e), Notifier()
    e.infer, e.backing = Infer(), Backing()
    return e


TEXT = f"shots fired, {SAID} at the {PLACE}, shots fired"


def test_a_transcript_through_feed_with_locate_the_model_and_the_listen_link_failing(log_sentinel):
    e = live_engine(log_sentinel.log)

    class Link:
        link = staticmethod(boom)
    e.listen = Link()
    evs = e.feed(row("t1", TEXT, 1000))
    assert evs and e.notifier.sent                              # pushed all the same, without its link
    lines = assert_no_sensitive(log_sentinel, SENSITIVE)
    for said in ("intel: locate: RuntimeError", "intel: inference: RuntimeError", "intel: model: RuntimeError",
                 "intel: listen link: RuntimeError"):
        assert any(said in x for x in lines), (said, lines)


def test_a_listen_clip_that_fails_on_the_listen_thread_logs_its_type_only(log_sentinel):
    e = live_engine(log_sentinel.log, locate=None)
    lk = L.Listen(Bucket(), lambda sources, t0, t1: [{"id": "t1", "start": 1000.0, "tier": "alert",
                                                      "source": "p25:mrrs:9211"}],
                  boom, settings=lambda: {"listen": True, "listen_before_s": 30, "listen_after_s": 30,
                                          "listen_expiry_min": 60, "listen_kbps": 32},
                  clock=Clock(1001), log=log_sentinel.log, ffmpeg="", tiers=lambda ids: {}, worker=False)
    e.listen = lk
    assert e.feed(row("t1", TEXT, 1000)) and e.notifier.sent
    t = threading.Thread(target=lk.drain, name="listen")    # its work on a thread of its own, as live (Listen._run)
    t.start()
    t.join(10)
    assert not t.is_alive()
    lines = assert_no_sensitive(log_sentinel, SENSITIVE)
    assert any("listen: making a link failed (RuntimeError)" in x for x in lines), lines
    assert not any("Traceback" in x for x in lines), lines


def test_the_sentinel_itself_catches_every_path(log_sentinel):
    """The sentinel isn't blind: a print, the app's log(), the logging module, stderr and a thread's uncaught exception
    are each caught."""
    import logging
    import sys

    def check(fn):
        fn()
        with pytest.raises(AssertionError, match="reached a log line"):
            assert_no_sensitive(log_sentinel, SENSITIVE)
        log_sentinel._seen.clear()

    check(lambda: print(f"x {PHRASE}"))
    check(lambda: log_sentinel.log(f"y {TOWN.upper()}"))
    check(lambda: logging.getLogger("sizeup.any").warning("z %s", SAID))
    check(lambda: sys.stderr.write(f"{PLACE}\n"))

    def thread():
        t = threading.Thread(target=boom, name="zzq")
        t.start()
        t.join(5)
    check(thread)
    assert_no_sensitive([f"{SAID[:-1]} nothing"], SENSITIVE)          # a list of lines (a component's log=) works too



def _ours():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "sizeup"
    return [*sorted((root / "intel").glob("*.py")),
            *(p for p in (root / "notify.py", root / "context_bundle.py", root / "talkers.py") if p.exists())]


def test_no_intel_or_notify_log_call_carries_an_exceptions_text():
    """The static backstop (tests/log_sentinel.py exception_text_in_logs): no log call in sizeup/intel, notify.py (or
    the modules beside them) reads an exception but for its type, status or code."""
    assert exception_text_in_logs(_ours()) == []


BAD = {
    "nested parens": "def f():\n    try:\n        g()\n    except ValueError as e:\n        log(f'x ({str(e)[:40]})')\n",
    "format spec": "def f():\n    try:\n        g()\n    except ValueError as e:\n        self.log(f'{e:>10}')\n",
    "an attribute": "def f():\n    try:\n        g()\n    except OSError as e:\n        log(f'{e.reason}')\n",
    "% format": "def f():\n    try:\n        g()\n    except OSError as e:\n        print('x %s' % e)\n",
    "logging args": "def f():\n    try:\n        g()\n    except OSError as e:\n        logger.warning('x %s', e)\n",
    ".format": "def f():\n    try:\n        g()\n    except OSError as err:\n        sys.stderr.write('x {}'.format(err))\n",
    "via a name": "def f():\n    try:\n        g()\n    except OSError as e:\n        msg = f'{e}'\n    log(msg)\n",
    "via two names": "def f():\n    try:\n        g()\n    except OSError as e:\n        m = repr(e)\n    m2 = 'x' + m\n    self.log(m2)\n",
    "via an attribute": ("def f(self):\n    for _ in range(3):\n        try:\n            g()\n        except OSError as exc:\n"
                         "            self.last = str(exc)\n    self.log(f'not sent ({self.last})')\n"),
    "another name": "def f():\n    try:\n        g()\n    except Exception as ex:\n        engine.log(repr(ex))\n",
    "a keyword": "def f():\n    try:\n        g()\n    except Exception as e:\n        emit_error(msg=f'{e}')\n",
    ".exception()": "def f():\n    try:\n        g()\n    except Exception:\n        logger.exception('x')\n",
    "exc_info=": "def f():\n    try:\n        g()\n    except Exception:\n        log.info('x', exc_info=True)\n",
    "a module's own": "try:\n    g()\nexcept ImportError as e:\n    print(f'no module: {e}')\n",
}

GOOD = {
    "its type": "def f():\n    try:\n        g()\n    except ValueError as e:\n        log(f'x ({type(e).__name__})')\n",
    "status and code": ("def f():\n    try:\n        g()\n    except OSError as e:\n"
                        "        code = str(getattr(e, 'code', '') or type(e).__name__)\n        log(f'{e.status} {code}')\n"),
    "another e outside": ("def f(evs):\n    try:\n        g()\n    except ValueError as e:\n        log(type(e).__name__)\n"
                          "    for e in evs:\n        log(f'event {e.id}')\n"),
    "another function's msg": ("def f():\n    try:\n        g()\n    except OSError as e:\n        msg = str(e)\n"
                               "        raise X(msg)\n\ndef h(msg):\n    log(msg)\n"),
    "an exception raised": "def f():\n    try:\n        g()\n    except OSError as e:\n        raise NotifyError(f'x {e}') from None\n",
}


@pytest.mark.parametrize("name", list(BAD))
def test_the_static_check_catches(name):
    assert exception_text_in_logs([(name, BAD[name])]), name


@pytest.mark.parametrize("name", list(GOOD))
def test_the_static_check_lets_be(name):
    assert exception_text_in_logs([(name, GOOD[name])]) == [], name


# -- the push itself (sizeup/notify.py): an intel pre-alert's title holds the town or the highway ------------------------

@pytest.mark.parametrize("fails", ["http", "unreachable", "protected"])
def test_a_push_not_sent_logs_its_lane_and_length_never_the_title(log_sentinel, monkeypatch, fails):
    import io
    from urllib.error import HTTPError, URLError

    from sizeup import notify

    def urlopen(req, timeout=None):
        if fails == "http":
            raise HTTPError("https://ntfy.example/", 429, f"too many {PLACE}", {},
                            io.BytesIO(f'{{"error": "limit for {PLACE} at {TOWN}"}}'.encode()))
        raise URLError(f"no route to {PLACE} ({TOWN})")
    monkeypatch.setattr(notify, "urlopen", urlopen)
    prot = (lambda: ["zzq-sentinel-topic-protected-0001"]) if fails == "protected" else None
    topic = "zzq-sentinel-topic-protected-0001" if fails == "protected" else "zzq-sentinel-test-topic-0001"
    n = notify.Notifier("https://ntfy.sh", topic, log=log_sentinel.log, sleep=lambda s: None, lane="intel")
    if prot:
        n.protected = prot
    outcome = []
    title = f"PD · shots fired · {PLACE}, {TOWN}"
    assert n.send(title, f"{SAID} at {PLACE}", done=outcome.append) and n.flush(5)
    lines = assert_no_sensitive(log_sentinel, SENSITIVE)
    assert any(f"notify: not sent ({n.last_error}): intel, {len(title)} chars" in x for x in lines), lines
    assert n.last_error == {"http": "HTTP 429", "unreachable": "unreachable (URLError)",
                            "protected": "a protected topic"}[fails]
    assert outcome and outcome[0]["error"] == n.last_error and outcome[0]["ok"] is False
