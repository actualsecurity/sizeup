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
from tests.log_sentinel import assert_no_sensitive
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


def test_no_intel_log_call_quotes_an_exceptions_text():
    """A static backstop: no log call in sizeup/intel (or the modules beside it) puts an exception's text in the line
    ({e}, {err}, str(e), repr(e), e.args): its type name only."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "sizeup"
    files = sorted((root / "intel").glob("*.py")) + [p for p in (root / "context_bundle.py", root / "talkers.py")
                                                     if p.exists()]
    call = re.compile(r"\b(?:log|print|warning|error|info|debug|exception)\((?:[^()]|\([^()]*\))*\)", re.S)
    quoted = re.compile(r"\{(?:e|err|exc|ex)(?:![rs])?\}|\b(?:str|repr)\((?:e|err|exc|ex)\)|\b(?:e|err|exc|ex)\.args")
    bad = [f"{p.name}: {m.group(0)[:120]}" for p in files for m in call.finditer(p.read_text()) if quoted.search(m.group(0))]
    assert bad == []
