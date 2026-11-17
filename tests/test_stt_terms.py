"""Private term lists per kind of source (sizeup/stt_terms.py): synthetic lists only, never the real ones."""

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import stt_terms
from sizeup.stt import Transcriber, prompt_echo
from sizeup.stt_terms import (DirFiles, Pushed, S3Files, StationPush, TermSet, TermStore, TermVocabularies, aws_phrases,
                              build_sets, est_tokens, merge, own_for, parse, version, vocab_name, vocab_v_of, with_terms)
from sizeup.transcribe import MAX_BYTES, MAX_PHRASE, MAX_PHRASES, AwsTranscriber, Settings, Usage

# made-up places and phrasing in the files' shape (sections, priority order, numbers as said)
FIRE = """# county_fire: made up for the tests
# built by nothing
# units
Engine sixty
Ladder ninety eight
Tower Ladder two
# alarms
working fire
second alarm
all hands
"""
PLACES = """# places: made up
# built by nothing
# towns
Zephyrton
Quillbrook
# streets
Marrowind Avenue
Example Post Road
Larkspire Lane
"""
PD = "# pd\n# x\nten four\nrespond to\nheadquarters\n"


def write(d, **files):
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / f"{name}.txt").write_text(text)
    return d


def wait(cond, s=5.0):
    end = time.time() + s
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


# -- the lists -----------------------------------------------------------------------------------------------------


def test_a_list_is_parsed_in_order_without_comments_or_repeats():
    text = "# a\n# b\n\n  Engine   sixty \nengine sixty\nI-95\nI 95\n# section\nCafé Row\n" + "x" * 101 + "\n"
    assert parse(text) == ["Engine sixty", "I-95", "Café Row"]


def test_which_list_each_source_takes():
    assert own_for("p25:mrrs:9211") == own_for("p25:mrrs:9212") == "pd_dispatch"
    assert own_for("p25:mrrs:9570") == "ems"
    assert own_for("fm:South 2", "fire", "paging") == own_for("South 2", "", "paging") == "county_fire"
    assert own_for("fm:FG1", "fire", "fireground") == own_for("p25:westchester:307", "fire") == "county_fire"
    assert own_for("air:HPN Tower", "air", "airband") == own_for("HPN Ground", "", "airband") == "airband"
    assert own_for("p25:westchester:333", "ems") == "ems"
    assert own_for("p25:westchester:383", "police") == "pd_dispatch"
    assert own_for("p25:mrrs:9001", "transit") is None and own_for("p25:mrrs:9002", "other") is None


def test_places_are_woven_into_the_top_of_each_list():
    own = [f"own {i}" for i in range(20)]
    places = [f"place {i}" for i in range(20)]
    m = merge(own, places, 0.35)
    assert len(m) == 40 and m[:3] == ["own 0", "place 0", "own 1"]
    top = m[:20]                                         # a smaller budget takes both, each in its own order
    assert 6 <= sum(t.startswith("place") for t in top) <= 8
    assert [t for t in m if t.startswith("own")] == own and [t for t in m if t.startswith("place")] == places
    assert merge(["a", "B"], ["b", "c"]) == ["a", "b", "c"]          # a place already in the list: once
    assert merge(["a"], []) == ["a"] and merge([], ["p"]) == ["p"]


def test_a_sets_version_follows_its_terms_not_its_comments():
    a = build_sets({"county_fire": parse(FIRE), "places": parse(PLACES)})
    b = build_sets({"county_fire": parse(FIRE.replace("# alarms", "# alarms and more")), "places": parse(PLACES)})
    c = build_sets({"county_fire": parse(FIRE + "fifth bell\n"), "places": parse(PLACES)})
    assert set(a) == {"county_fire"} and a["county_fire"].v == b["county_fire"].v != c["county_fire"].v
    ts = a["county_fire"]
    assert ts.vocab_v == f"county_fire@{ts.v}" and len(ts.v) == 12 and ts.places
    assert "Engine sixty" in ts.terms and "Zephyrton" in ts.terms
    alone = build_sets({"county_fire": parse(FIRE)})["county_fire"]       # no places.txt: its own terms only
    assert not alone.places and alone.terms == tuple(parse(FIRE))
    assert build_sets({"places": parse(PLACES)}) == {}                   # places alone biases nothing


# -- whisper's prompt ----------------------------------------------------------------------------------------------


def test_the_prompt_takes_the_top_terms_within_whispers_budget():
    many = tuple(f"Marrowind {i} Avenue" for i in range(400))
    ts = TermSet("county_fire", many, version(many))
    text, vv = with_terms("", ts)
    assert vv == ts.vocab_v and text.endswith("Marrowind 1 Avenue, Marrowind 0 Avenue.")      # the top nearest the audio
    assert est_tokens(text) <= stt_terms.PROMPT_TOKENS < 224             # whisper keeps 224 tokens of a prompt
    assert len(text) < 224 * 4                                             # (and a real BPE averages ~4 chars a token)
    base = "77 Alpha 3, 60 Control."
    text2, _ = with_terms(base, ts)
    assert text2.endswith(" " + base) and est_tokens(text2) <= stt_terms.PROMPT_TOKENS   # the source's own prompt last
    assert text2.count("Marrowind") < text.count("Marrowind")             # ... and the terms fit in what it leaves
    full = "word " * 300
    assert with_terms(full, ts) == (full.strip(), "")                       # no room: the prompt as it was, no version
    assert with_terms(base, None) == (base, "")
    assert est_tokens("Mamaroneck") >= 3 and est_tokens("ten four.") >= 3


def test_prompt_echo_is_whisper_reading_its_prompt_back():
    p = "Engine sixty, Ladder ninety eight, Tower Ladder two, working fire, Zephyrton, Marrowind Avenue. 77 Alpha 3."
    assert prompt_echo("Engine sixty, Ladder ninety eight, Tower Ladder two, working fire.", p)
    assert prompt_echo("tower ladder two working fire zephyrton marrowind avenue", p)
    assert not prompt_echo("Engine sixty on location, working fire, second floor", p)    # a few of its words: speech
    assert not prompt_echo("Ladder ninety eight", p) and not prompt_echo("anything", "")


class _Item(SimpleNamespace):
    pass


def _run_one(prompt: str, said: str, audio: np.ndarray, **item):
    """One clip through the station's Transcriber with a whisper that answers `said` (or reads its prompt back)."""
    done = []
    client = SimpleNamespace(transcribe=lambda a, p="", **kw: ((p if said is None else said), 0.4))
    tr = Transcriber(client, done.append, prompt=lambda it: prompt)
    tr.start()
    it = _Item(**{"fm": False, **item})
    tr.submit(it, audio)
    assert wait(lambda: done)
    return done[0]


def test_a_silent_clip_with_a_prompt_never_gets_the_prompt_as_its_text():
    p = ("Engine sixty, Ladder ninety eight, Tower Ladder two, working fire, second alarm, all hands, Zephyrton, "
         "Quillbrook, Marrowind Avenue, Example Post Road.")
    silent = np.zeros(16000 * 3, np.float32)
    it = _run_one(p, None, silent, low_energy=True)                        # whisper parrots the whole prompt
    assert it.status == "no_speech" and it.raw == ""
    it = _run_one(p, "Tower Ladder two, working fire, second alarm, all hands.", silent, low_energy=True)
    assert it.status == "no_speech" and it.raw == ""                       # ... or a stretch of it
    it = _run_one(p, None, np.zeros(16000 * 4, np.float32))                # not flagged quiet: too many words for 4 s
    assert it.status == "no_speech"
    # a mic click: a few of the prompt's words in any order (as large-v3-turbo did with a police prompt)
    it = _run_one(p, "Quillbrook, five, Zephyrton.", np.zeros(16000 * 3, np.float32), low_energy=True)
    assert it.status == "no_speech"
    it = _run_one(p, "Quillbrook, five, Zephyrton.", np.zeros(16000 * 3, np.float32))     # a voice there: kept
    assert it.status == "done"
    it = _run_one(p, "We'll be right back.", np.zeros(16000 * 3, np.float32), low_energy=True)   # stock, on dead air
    assert it.status == "no_speech"
    # speech that names a few of the prompt's words keeps them
    it = _run_one(p, "Engine sixty on location, working fire.", np.zeros(16000 * 4, np.float32))
    assert it.status == "done" and it.raw == "Engine sixty on location, working fire."
    # ... and so does a unit list said at radio pace (in the prompt's order, but a person saying it)
    it = _run_one(p, "Engine sixty, Ladder ninety eight, Tower Ladder two.", np.zeros(16000 * 5, np.float32))
    assert it.status == "done"


# -- where the lists are read from ---------------------------------------------------------------------------------


def test_a_folder_of_lists_is_read_again_only_when_one_changed(tmp_path):
    d = write(tmp_path / "vocab" / "terms", county_fire=FIRE, places=PLACES, pd_dispatch=PD)
    reads = []
    store = TermStore(DirFiles(tmp_path / "vocab"), log=lambda m: None, on_read=lambda s, ch: reads.append(ch))
    assert store.v == "" and store.get("county_fire") is None
    assert store.refresh() and set(store.sets()) == {"county_fire", "pd_dispatch"}
    v1 = store.v
    assert not store.refresh() and reads == [True, False] and store.v == v1
    time.sleep(0.01)
    (d / "county_fire.txt").write_text(FIRE + "fifth bell\n")
    assert store.refresh() and store.v != v1 and "fifth bell" in store.get("county_fire").terms
    (d / "pd_dispatch.txt").unlink()                                        # a list taken away: its set goes
    assert store.refresh() and store.get("pd_dispatch") is None
    st = store.state()
    assert st["sets"]["county_fire"]["terms"] == len(store.get("county_fire").terms)
    assert "Engine" not in json.dumps(st)                                   # counts and versions, never terms


class FakeS3:
    """GET with If-None-Match, as S3 answers it."""

    def __init__(self, objects):
        self.objects, self.calls = objects, []

    def send(self, method, url, headers, timeout):
        key = url.split(".amazonaws.com/", 1)[1]
        self.calls.append((key, headers.get("If-None-Match")))
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256") and "/s3/aws4_request" in headers["Authorization"]
        if key not in self.objects:
            return 404, {}, b"<Error><Code>NoSuchKey</Code></Error>"
        body = self.objects[key]
        etag = f'"{hash(body) & 0xffffff:x}"'
        if headers.get("If-None-Match") == etag:
            return 304, {"ETag": etag}, b""
        return 200, {"ETag": etag}, body


def test_the_hub_reads_the_lists_from_its_backups_bucket(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    files = stt_terms.files_from({"LITESTREAM_BUCKET": "example-backups", "LITESTREAM_PATH": "sizeup-hub"}, "/ignored")
    assert isinstance(files, S3Files) and files.where() == "s3://example-backups/sizeup-hub/vocab/"
    s3 = FakeS3({"sizeup-hub/vocab/county_fire.txt": FIRE.encode(), "sizeup-hub/vocab/places.txt": PLACES.encode()})
    files.send = s3.send
    store = TermStore(files, log=lambda m: None)
    assert store.refresh() and set(store.sets()) == {"county_fire"}
    assert not store.refresh()
    assert all(tag for k, tag in s3.calls[len(stt_terms.NAMES):] if k.endswith(("county_fire.txt", "places.txt")))
    s3.objects["sizeup-hub/vocab/county_fire.txt"] = b"\xff\xfe not text"
    assert not store.refresh() and "county_fire: not UTF-8" in store.error and store.get("county_fire") is not None
    files.send = lambda *a: (403, {}, b"<Error><Code>AccessDenied</Code></Error>")
    assert not store.refresh() and "AccessDenied" in store.error and store.get("county_fire") is not None
    assert stt_terms.files_from({}, "") is None
    assert isinstance(stt_terms.files_from({}, "/some/dir"), DirFiles)


# -- the station's copy --------------------------------------------------------------------------------------------


def test_the_station_keeps_what_the_hub_sends_and_reports_its_version(tmp_path):
    write(tmp_path, county_fire=FIRE, places=PLACES)
    store = TermStore(DirFiles(tmp_path), log=lambda m: None)
    store.refresh()
    payload = store.payload()
    station = Pushed()
    assert station.state() == {"v": "", "sets": {}}
    st = station.apply(json.loads(json.dumps(payload)))
    assert st["v"] == store.v and station.get("county_fire").vocab_v == store.get("county_fire").vocab_v
    assert station.get("county_fire").terms == store.get("county_fire").terms[:stt_terms.PUSH_TERMS]
    for bad in ({"sets": {"Bad Name": {"terms": [], "v": "0" * 12}}}, {"sets": {"ems": {"terms": "x", "v": "0" * 12}}},
                {"sets": {"ems": {"terms": ["a"], "v": "nope"}}}, {"sets": {"ems": {"terms": ["x" * 101], "v": "0" * 12}}}, []):
        with pytest.raises(ValueError):
            station.apply(bad)
    assert station.v == store.v                                             # a refused one keeps the copy before
    assert station.apply({"v": "", "sets": {}}) == {"v": "", "sets": {}}  # the hub turned it off


def test_the_hub_sends_the_station_a_new_copy_only_when_it_differs():
    sent = []
    want = {"v": "a" * 12, "sets": {}}
    gate = threading.Event()
    push = StationPush(lambda: want, lambda p: (sent.append(p), gate.wait(2)), log=lambda m: None)
    assert not push.check({"v": "a" * 12})                                  # the same: nothing
    assert not push.check({"v": ""}, connected=False)
    assert push.check({"v": ""}) and not push.check({"v": ""})              # one at a time
    gate.set()
    assert wait(lambda: not push._busy) and sent == [want]
    assert not push.check(None) or True                                      # (a station from before this: tried)
    errors = []
    push2 = StationPush(lambda: want, lambda p: (_ for _ in ()).throw(RuntimeError("not allowed")), log=errors.append)
    assert push2.check(None) and wait(lambda: push2.error)
    assert not push2.check(None)                                            # a minute before it tries again
    assert len(errors) == 1
    empty = StationPush(lambda: {"v": "", "sets": {}}, sent.append, log=lambda m: None)
    assert not empty.check(None) and not empty.check({"v": ""})             # nothing to send: an older station is fine


# -- Amazon Transcribe ---------------------------------------------------------------------------------------------


class FakeApi:
    """Transcribe's vocabulary calls: PENDING for `pending` GetVocabulary calls, then READY."""

    def __init__(self, pending=3):
        self.vocabs, self.calls, self.pending = {}, [], pending

    def __call__(self, service, target, body, region, endpoint=""):
        from sizeup.aws import AwsError
        op, name = target.removeprefix("Transcribe."), body.get("VocabularyName")
        self.calls.append((op, name))
        v = self.vocabs.get(name)
        if op == "GetVocabulary":
            if v is None:
                raise AwsError("not found", "NotFoundException", 400)
            v["polls"] += 1
            if v["polls"] > self.pending:
                v["state"] = "READY"
            return {"VocabularyState": v["state"]}
        if op == "CreateVocabulary":
            assert v is None
            ph = body["Phrases"]
            assert len(ph) <= MAX_PHRASES and sum(len(p.encode()) + 1 for p in ph) <= MAX_BYTES
            assert all(len(p) <= MAX_PHRASE and not any(c.isdigit() or c == " " for c in p) and p.isascii() for p in ph)
            self.vocabs[name] = {"state": "PENDING", "phrases": ph, "polls": 0}
            return {"VocabularyState": "PENDING"}
        if op == "DeleteVocabulary":
            self.vocabs.pop(name, None)
            return {}
        raise AssertionError(op)


def _sets(extra=""):
    big = "\n".join(f"Marrowind {chr(97 + i % 26)}{chr(97 + i // 26 % 26)}{chr(97 + i // 676)} Lane" for i in range(3000))
    return build_sets({"county_fire": parse(FIRE + extra), "places": parse(PLACES + big + "\nCafé Row\n")})


def test_each_list_gets_its_own_vocabulary_named_by_its_version():
    kv, api, leader = {}, FakeApi(), [True]
    tv = TermVocabularies("us-east-1", kv.get, kv.__setitem__, call=api, log=lambda m: None,
                          leader=lambda: leader[0], poll_s=0.05)
    s1 = _sets()
    name1 = vocab_name(s1["county_fire"])
    assert name1 == f"sizeup-terms-county_fire-{s1['county_fire'].v}" and vocab_v_of(name1) == s1["county_fire"].vocab_v
    assert tv.ready("county_fire") == ""
    tv.sync(s1)
    assert wait(lambda: tv.ready("county_fire") == name1)
    ph = api.vocabs[name1]["phrases"]
    assert ph[:2] == ["Engine-sixty", "Zephyrton"] and "Cafe-Row" not in ph[:10]     # the top first, within AWS's limits
    assert any("Marrowind" in p for p in ph) and len(ph) < len(s1["county_fire"].terms)
    n = len(api.calls)
    tv.sync(s1)                                                          # the same version: nothing sent
    assert len(api.calls) == n
    leader[0] = False
    s2 = _sets("fifth bell\n")
    tv.sync(s2)                                                          # not the leader: the leader makes it
    assert tv.ready("county_fire") == name1 and len(api.calls) == n
    leader[0] = True
    tv.sync(s2)
    name2 = vocab_name(s2["county_fire"])
    assert tv.ready("county_fire") == name1                              # the one before, while the next is PENDING
    assert wait(lambda: tv.ready("county_fire") == name2)
    for i in range(3):                                                   # a few versions on: the oldest deleted
        tv.sync(_sets(f"alarm number {'x' * (i + 1)}\n"))
        assert wait(lambda: tv.ready("county_fire") == tv.meta("county_fire")["want"])
    assert name1 not in api.vocabs and len(tv.meta("county_fire")["names"]) == stt_terms.KEEP
    st = tv.state()["county_fire"]
    assert st["state"] == "READY" and st["phrases"] > 0 and "Engine" not in json.dumps(st)


def test_a_vocabulary_aws_refuses_leaves_the_channel_on_its_own():
    kv, leader = {}, [True]

    def refuse(service, target, body, region, endpoint=""):
        from sizeup.aws import AwsError
        if target.endswith("GetVocabulary"):
            raise AwsError("not found", "NotFoundException", 400)
        raise AwsError("AccessDeniedException: not allowed", "AccessDeniedException", 400)

    tv = TermVocabularies("us-east-1", kv.get, kv.__setitem__, call=refuse, log=lambda m: None, poll_s=0.01)
    tv.sync(_sets())
    assert tv.ready("county_fire") == "" and tv.state()["county_fire"]["state"] == "error"


def test_aws_phrases_drop_accents_and_keep_the_rest():
    assert aws_phrases(["Café Row", "ten four", "日本"]) == ["Cafe Row", "ten four"]


class _Job(SimpleNamespace):
    pass


def test_a_term_vocabulary_goes_ahead_of_the_channels_own_and_its_version_is_recorded(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    t = AwsTranscriber(Settings(), Usage(lambda k: None, lambda k, v: None), lambda *a: None, lambda *a: None,
                       log=lambda m: None)
    t.vocabulary, t.channel_vocabulary["p25:mrrs:9211"] = "sizeup-pcfd", "sizeup-pd"
    assert t.vocab_for("FG1") == "sizeup-pcfd" and t.vocab_for("p25:mrrs:9211") == "sizeup-pd"
    t.term_vocabulary = lambda ch: "sizeup-terms-county_fire-0123456789ab" if ch == "FG1" else ""
    assert t.vocab_for("FG1") == "sizeup-terms-county_fire-0123456789ab" and t.vocab_for("p25:mrrs:9211") == "sizeup-pd"
    assert "vocabulary-name=sizeup-terms-county_fire-0123456789ab" in t.url("FG1")
    t.term_vocabulary = lambda ch: 1 / 0                                  # trouble there: the channel's own
    assert t.vocab_for("FG1") == "sizeup-pcfd"
    from sizeup.transcribe import Job
    got = []
    t.on_done = lambda item, text, conf: got.append((item.vocab_v, text))
    t.vocab_version = vocab_v_of
    item = _Job(vocab_v="")
    job = Job("FG1", item, np.zeros(10, np.int16), vocab="sizeup-terms-county_fire-0123456789ab")
    t._open.add(job)
    t._done(job, "words", 0.9)
    assert got == [("county_fire@0123456789ab", "words")]


# -- the app: the prompt, the vocabulary, the row's vocab_v ------------------------------------------------------


def _app(tmp_path, **kw):
    from tests.test_app import cfg
    from sizeup.app import App
    write(tmp_path / "terms", county_fire=FIRE, places=PLACES, pd_dispatch=PD)
    return App(replace(cfg(tmp_path), p25_talkgroups={307: "Fire/EMS 15"}, stt_terms_dir=str(tmp_path / "terms"), **kw))


def test_whispers_prompt_has_the_sources_terms_and_the_row_says_which(tmp_path, monkeypatch):
    from sizeup.radio.p25 import Call
    from tests.test_app import FakeWhisper, voice
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    seen = []

    class Spy(FakeWhisper):
        def transcribe(self, audio, prompt=""):
            seen.append(prompt)
            return "Engine sixty on location", 0.9

    app = _app(tmp_path, stt_terms_whisper=True, stt_talker_prompt="ids")
    app.stt_terms.refresh()
    app.stt.client = Spy({})
    app.stt.start()
    now = time.time()
    app._call(Call("307-t", 307, "Fire/EMS 15", now, now + 2, ["77A3"], voice(2.0), ""))
    assert wait(lambda: seen)
    ts = app.stt_terms.get("county_fire")
    assert seen[0].endswith("Ladder ninety eight, Zephyrton, Engine sixty. 77 Alpha 3.")   # the top last, then the talkers
    row = app.cad.store.one("SELECT text, vocab_v FROM radio_traffic WHERE id = ?", ("307-t",))
    assert wait(lambda: (app.cad.store.one("SELECT vocab_v FROM radio_traffic WHERE id = ?", ("307-t",)) or {})
                .get("vocab_v") == ts.vocab_v), row
    # another system's police: its own list (and its own profile prompt, after it)
    pd = SimpleNamespace(source="PD", source_key="p25:mrrs:9211", service="police", unit="", start=None, vocab_v="")
    p = app._prompt(pd)
    assert "Zephyrton, ten four. Police dispatch." in p and pd.vocab_v == app.stt_terms.get("pd_dispatch").vocab_v
    assert p.endswith(app._base_prompt(pd, short=True)) and len(app._base_prompt(pd, short=True)) < len(app._base_prompt(pd))
    # a source with no list (transit): the prompt as before, no version
    bus = SimpleNamespace(source="Bus", source_key="p25:mrrs:9001", service="transit", unit="", start=None, vocab_v="x")
    assert app._prompt(bus) == "" and bus.vocab_v == ""


def test_switched_off_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    app = _app(tmp_path)                                                     # terms_whisper and terms_aws off
    assert app.stt_terms is None and app.term_vocabs is None
    item = SimpleNamespace(source="FG1", source_key="FG1", service="fire", unit="", start=None)
    assert app._prompt(item) == "" and item.vocab_v == ""
    assert app.stt_terms_payload() == {"v": "", "sets": {}}


def test_a_station_prompts_with_what_the_hub_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    hub = _app(tmp_path / "hub", stt_terms_whisper=True)
    hub.stt_terms.refresh()
    from tests.test_app import cfg
    from sizeup.app import App
    station = App(replace(cfg(tmp_path / "st"), cloud_role="station"))
    assert station.stt_terms_applied() == {"v": "", "sets": {}}
    item = SimpleNamespace(source="South 2", source_key="", service="", unit="", start=None, kind="page")
    assert station._prompt(item) == ""
    st = station.set_stt_terms(json.loads(json.dumps(hub.stt_terms_payload())))
    assert st["v"] == hub.stt_terms.v and station.stt_terms_applied()["v"] == hub.stt_terms.v
    assert station._prompt(item) == hub._prompt(SimpleNamespace(**vars(item))) != ""
    assert item.vocab_v == hub.stt_terms.get("county_fire").vocab_v
    from sizeup.cloud.station import CALLS
    assert "set_stt_terms" in CALLS


def test_the_radio_log_keeps_the_version_with_the_text(tmp_path):
    from tests.test_app import cfg
    from sizeup.app import App
    app = App(cfg(tmp_path))
    comms = app.cad.comms
    comms.record({"id": "fg1-1", "start": 1.0, "end": 2.0, "source": "FG1", "talkgroup": None, "text": "words",
                  "status": "done", "model": "m", "vocab_v": "county_fire@0123456789ab"})
    q = lambda: app.cad.store.one("SELECT text, vocab_v FROM radio_traffic WHERE id = 'fg1-1'")
    assert q()["vocab_v"] == "county_fire@0123456789ab"
    comms.retext("fg1-1", "other words", "done", "aws", vocab_v="sizeup-pcfd@abcdefabcdef")
    assert dict(q()) == {"text": "other words", "vocab_v": "sizeup-pcfd@abcdefabcdef"}
    comms.retext("fg1-1", "words again", "done", "whisper")              # a text with no version: none, not the old one
    assert q()["vocab_v"] == ""


def test_an_aws_transcript_records_its_vocabulary(tmp_path):
    from tests.test_app import cfg
    from sizeup.app import App, AwsJob
    app = App(cfg(tmp_path))
    app.cad.comms.record({"id": "fg1-2", "start": 1.0, "end": 2.0, "source": "FG1", "talkgroup": None, "text": "",
                          "status": "pending", "model": "aws"})
    app._aws_done(AwsJob("traffic", "fg1-2", "FG1", vocab_v="county_fire@0123456789ab"), "Engine sixty", 0.9)
    assert app.cad.store.one("SELECT vocab_v FROM radio_traffic WHERE id = 'fg1-2'")["vocab_v"] == "county_fire@0123456789ab"
    app.vocab = SimpleNamespace(name="sizeup-pcfd", state=lambda: {"sig": "abcdefabcdef"})
    assert app._vocab_version("sizeup-pcfd") == "sizeup-pcfd@abcdefabcdef"
    assert app._vocab_version("sizeup-terms-ems-0123456789ab") == "ems@0123456789ab"


def test_the_config_takes_the_switches(tmp_path):
    from sizeup.config import ConfigError, load
    base = (tmp_path / "c.toml")
    base.write_text('[stt]\nterms_whisper = true\nterms_aws = true\nterms_dir = "~/vocab"\n')
    c = load(base)
    assert c.stt_terms_whisper and c.stt_terms_aws and c.stt_terms_dir.endswith("/vocab") and "~" not in c.stt_terms_dir
    base.write_text('[stt]\nterms_aws = "yes"\n')
    with pytest.raises(ConfigError):
        load(base)
