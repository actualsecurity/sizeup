"""Private term lists per kind of source (sizeup/stt_terms.py): synthetic lists only, never the real ones."""

import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from sizeup import stt_terms
from sizeup import stt
from sizeup.stt import Transcriber, prompt_echo, prompt_only
from sizeup.stt_terms import (DirFiles, Pushed, S3Files, StationPush, TermSet, TermStore, TermVocabularies, aws_phrases,
                              build_sets, est_tokens, merge, own_for, parse, prompt_terms, version, vocab_v_of, with_terms,
                              written)
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


def test_an_admins_service_choice_picks_the_list():
    """Settings > Radios' service override (sourcekeys.set_service_overrides) is the source's service here too."""
    from sizeup import sourcekeys
    before = sourcekeys.service_overrides()
    try:
        sourcekeys.set_service_overrides({"p25:westchester:333": "police", "p25:mrrs:9001": "ems",
                                          "tg:4242": "fire"})
        assert own_for("p25:westchester:333", "ems") == "pd_dispatch"     # the admin's police over the file's EMS
        assert own_for("tg:333", "ems") == "pd_dispatch"                   # (the county's older key the same)
        assert own_for("p25:mrrs:9001", "transit") == "ems"
        assert own_for("p25:westchester:4242", "other") == "county_fire"
        assert own_for("p25:mrrs:9211", "fire") == "pd_dispatch"          # no choice for it: the plan's own key
        sourcekeys.set_service_overrides({"p25:mrrs:9211": "fire", "p25:mrrs:9570": "transit",
                                          "fm:South 2": "police", "air:HPN Tower": "police"})
        assert own_for("p25:mrrs:9211", "police") == "county_fire"         # an admin's choice over the plan's keys
        assert own_for("p25:mrrs:9570", "ems") is None
        assert own_for("fm:South 2", "fire", "paging") == "county_fire"     # paging and airband: by their audio
        assert own_for("air:HPN Tower", "air", "airband") == "airband"
        assert own_for("p25:westchester:5", "police") == "pd_dispatch"     # no choice: as given
    finally:
        sourcekeys.set_service_overrides(before)


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


def test_whispers_prompt_writes_numbers_as_digits():
    """Spelled out in the prompt, whisper writes every number as words (real county traffic: digits in 15 of 120 speech
    transcripts with such a prompt, 69 without) and the dispatch parser finds no unit or house number."""
    for said, w in (("Engine one forty two", "Engine 142"), ("eighty eight Alpha three", "88 Alpha 3"),
                    ("ten thirty two", "10-32"), ("twenty seven sixty one", "2761"), ("four oh one", "401"),
                    ("squawk one two zero zero", "squawk 1200"), ("runway two seven", "runway 27"), ("three niner", "39"),
                    ("fifteen hundred", "1500"), ("ten four", "10-4"), ("Squad ten", "Squad 10"),
                    ("sixty Control", "60 Control"), ("Oh Street", "Oh Street"), ("second alarm", "second alarm"),
                    ("I-eighty seven", "I-87"), ("Marrowind Avenue", "Marrowind Avenue")):
        assert written(said) == w, said
    terms = ["Engine sixty", "working fire", "Ladder ninety eight", "Zephyrton", "ten four", "Engine 60"]
    assert prompt_terms(terms, max_numeric=10) == ["Engine 60", "working fire", "Ladder 98", "Zephyrton", "10-4"]
    assert prompt_terms(terms, max_numeric=1) == ["Engine 60", "working fire", "Zephyrton"]
    assert prompt_terms(terms, max_numeric=0) == ["working fire", "Zephyrton"]   # a callsign pulls a heard number to it
    ts = TermSet("county_fire", tuple(terms), version(terms))
    text, _ = with_terms("", ts)
    assert "sixty" not in text and "ninety" not in text                    # never a number spelled out


def test_the_prompt_takes_the_top_terms_within_whispers_budget():
    many = tuple(f"Marrowind {'abcdefghijklmnopqrstuvwxyz'[i % 26]}{'abcdefghijklmnopqrstuvwxyz'[i // 26 % 26]} Avenue"
                 for i in range(400))
    ts = TermSet("county_fire", many, version(many))
    text, vv = with_terms("", ts)
    assert vv == ts.vocab_v and text.endswith("Marrowind ba Avenue, Marrowind aa Avenue.")    # the top nearest the audio
    assert est_tokens(text) <= stt_terms.PROMPT_TOKENS < 224             # whisper keeps 224 tokens of a prompt
    assert len(text) < 224 * 4                                             # (and a real BPE averages ~4 chars a token)
    base = "77 Alpha 3, 60 Control."
    text2, _ = with_terms(base, ts)
    assert text2.endswith(" " + base) and est_tokens(text2) <= stt_terms.PROMPT_TOKENS   # the source's own prompt last
    assert text2.count("Marrowind") < text.count("Marrowind")             # ... and the terms fit in what it leaves
    full = "word " * 300
    assert with_terms(full, ts) == (full.strip(), "")                       # no room: the prompt as it was, no version
    assert with_terms(base, None) == (base, "")
    assert est_tokens("Mamaroneck") >= 3 and est_tokens("10-4.") >= 4


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
    p = ("Zephyrton, working fire, second alarm, all hands, Quillbrook, Marrowind Avenue, Example Post Road, "
         "Tower Ladder, Larkspire Lane.")
    terms = {"vocab_v": "county_fire@0123456789ab"}                        # a term list went in the prompt
    s = lambda secs: np.zeros(int(16000 * secs), np.float32)
    assert _run_one(p, None, s(3), **terms).status == "no_speech"          # the whole prompt, read back: too fast
    assert _run_one(p, None, s(8), **terms).status == "no_speech"
    assert _run_one(p, "working fire, second alarm, all hands.", s(1), **terms).status == "no_speech"   # short
    assert _run_one(p, "Quillbrook, Zephyrton, Larkspire Lane.", s(1), **terms).status == "no_speech"   # its items only
    assert _run_one(p, "Quillbrook, Zephyrton.", s(1), **terms).status == "done"        # two may be said
    hail = "Larkspire Lane, Port Chester, Zephyrton. Engine 61."                        # a town and the talker: a hail
    assert _run_one(hail, "Engine 61, Port Chester.", s(1.3), **terms).status == "done"
    assert _run_one(p, "We'll be right back.", s(1), **terms).status == "no_speech"     # turbo's, with a list
    # never on the energy alone (P25 has none; a re-run's and the FM fixture's are unreliable): real speech is kept
    assert _run_one(p, "Quillbrook, Zephyrton.", s(3), low_energy=True, **terms).status == "done"
    assert _run_one(p, "Quillbrook, copy.", s(1), **terms).status == "done"            # a word of its own
    assert _run_one(p, "Engine 60 on location, working fire.", s(2), **terms).status == "done"
    assert _run_one(p, "Tower Ladder, working fire, second alarm.", s(3), **terms).status == "done"   # at a pace
    assert _run_one(p, "Zephyrton", s(1), **terms).status == "done"                     # one item: may be said
    # a number of the prompt alone (the talker's ID), or a piece of it: on a dead-air key-up only
    pt = p + " 0633."
    assert _run_one(pt, "063.", s(1), low_energy=True, **terms).status == "no_speech"
    assert _run_one(pt, "063.", s(1), **terms).status == "done"           # a voice there ("191 responding" -> "191.")
    assert _run_one(pt, "45.", s(1), low_energy=True, **terms).status == "done"      # not the prompt's
    # no term list in the prompt: the guard is off (the prompt is the source's own, as before)
    assert _run_one(p, None, s(3)).status == "done"
    assert _run_one(p, "Quillbrook, Zephyrton.", s(1)).status == "done"
    assert prompt_only("Quillbrook and Zephyrton", p) == 2 and prompt_only("Quillbrook, five", p) == 0
    assert stt.PROMPT_ONLY_ITEMS == 3


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
    (d / "county_fire.txt").write_text(FIRE + "fifth bell\n")                # an upload under way: not used yet ...
    assert not store.refresh() and store.v == v1 and store.settling
    time.sleep(0.01)
    (d / "places.txt").write_text(PLACES + "Gullhaven\n")                    # ... the next file of it comes ...
    assert not store.refresh() and store.v == v1
    assert store.refresh() and store.v != v1 and not store.settling        # ... and a read that finds the same: used
    assert "fifth bell" in store.get("county_fire").terms and "Gullhaven" in store.get("county_fire").terms
    (d / "pd_dispatch.txt").unlink()                                        # a list taken away: its set goes
    assert not store.refresh() and store.get("pd_dispatch") is not None
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
    unread = StationPush(lambda: None, sent.append, log=lambda m: None)
    assert not unread.check({"v": "b" * 12})                                # the lists not read yet: never an empty set


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
        if op in ("CreateVocabulary", "UpdateVocabulary"):
            assert (v is None) == (op == "CreateVocabulary")
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


OWN_PCFD = ["60 Control", "Engine 61", "Fireground 1", "Example Post Road"]     # the channel's own (an admin's edits)


def _tv(kv, api, leader=(True,), own=None, clock=time.time):
    return TermVocabularies("us-east-1", kv.get, kv.__setitem__, lambda base: list((own or {}).get(base, [])),
                            call=api, log=lambda m: None, leader=lambda: leader[0], poll_s=0.05, clock=clock)


def test_a_channels_term_vocabulary_holds_its_own_vocabulary_first_then_the_list():
    """A stream takes one vocabulary: the term one must keep the channel's own phrases (Settings' edits included)."""
    kv, api, leader = {}, FakeApi(), [True]
    own = {"sizeup-pcfd": list(OWN_PCFD)}
    tv = _tv(kv, api, leader, own)
    s1 = _sets()
    tv.sync(s1)
    assert tv.ready("county_fire", "sizeup-pcfd") == "" and not api.calls      # no channel asked: nothing made
    tv.want("county_fire", "sizeup-pcfd")                                       # FG1 streamed
    assert wait(lambda: tv.ready("county_fire", "sizeup-pcfd"))
    name1 = tv.ready("county_fire", "sizeup-pcfd")
    assert name1.startswith("sizeup-terms-county_fire-pcfd-") and vocab_v_of(name1).startswith("county_fire+pcfd@")
    ph = api.vocabs[name1]["phrases"]
    assert ph[:4] == ["sixty-Control", "Engine-sixty-one", "Fireground-one", "Example-Post-Road"]   # its own first
    assert ph[4:6] == ["Engine-sixty", "Zephyrton"] and "Cafe-Row" not in ph[:10]  # then the list, within AWS's limits
    assert any("Marrowind" in p for p in ph) and len(ph) < len(s1["county_fire"].terms) + 4
    assert tv.respell(name1)["engine sixty"] == "Engine 60"                     # the list's numbers back to digits
    assert tv.respell(name1)["engine sixty one"] == "Engine 61"                 # ... and the own phrases'
    n = len(api.calls)
    tv.sync(s1)                                                          # the same: nothing sent
    assert len(api.calls) == n
    own["sizeup-pcfd"].append("Tower Ladder 2")                          # an admin edits the list: made again
    leader[0] = False
    tv.sync(s1)                                                          # (not the leader: the leader makes it)
    assert tv.ready("county_fire", "sizeup-pcfd") == name1 and len(api.calls) == n
    leader[0] = True
    api.pending = 10 ** 9                                                # (held PENDING until let go: no race)
    tv.sync(s1)
    name2 = tv.meta("county_fire", "sizeup-pcfd")["want"]
    assert name2 != name1 and tv.ready("county_fire", "sizeup-pcfd") == name1     # the one before while PENDING
    api.pending = 0
    assert wait(lambda: tv.ready("county_fire", "sizeup-pcfd") == name2)
    assert "Tower-Ladder-two" in api.vocabs[name2]["phrases"][:5] and tv.respell(name1) == {}
    for i in range(3):                                                   # a few versions on: the oldest deleted
        tv.sync(_sets(f"alarm number {'x' * (i + 1)}\n"))
        assert wait(lambda: tv.ready("county_fire", "sizeup-pcfd") == tv.meta("county_fire", "sizeup-pcfd")["want"])
    assert name1 not in api.vocabs and len(tv.meta("county_fire", "sizeup-pcfd")["names"]) == stt_terms.KEEP
    st = tv.state()["county_fire+pcfd"]
    assert st["state"] == "READY" and st["phrases"] > 0 and "Engine" not in json.dumps(st)
    # another own vocabulary (a context source with none): its own pair
    tv.want("county_fire", "")
    assert wait(lambda: tv.ready("county_fire", "").startswith("sizeup-terms-county_fire-none-"))
    tv.sync({})                                                          # the list taken away: its vocabularies go
    assert tv.ready("county_fire", "sizeup-pcfd") == "" and not any(n.startswith("sizeup-terms-") for n in api.vocabs)


def test_aws_refusing_leaves_the_channel_on_its_own_and_its_words_out_of_the_log():
    kv, logs, now = {}, [], [1000.0]

    class Refuse(FakeApi):
        def __call__(self, service, target, body, region, endpoint=""):
            from sizeup.aws import AwsError
            if target.endswith(("CreateVocabulary", "UpdateVocabulary")):
                self.calls.append((target, body["VocabularyName"]))
                raise AwsError("BadRequestException: Zephyrton is not a valid phrase", "BadRequestException", 400)
            return super().__call__(service, target, body, region, endpoint)

    api = Refuse()
    tv = TermVocabularies("us-east-1", kv.get, kv.__setitem__, call=api, log=logs.append, poll_s=0.01,
                          clock=lambda: now[0])
    tv.want("county_fire", "")
    tv.sync(_sets())
    assert tv.ready("county_fire", "") == "" and tv.state()["county_fire+none"]["state"] == "error"
    assert not any("Zephyrton" in m for m in logs) and "Zephyrton" not in json.dumps(kv)   # AWS's text never kept
    n = len(api.calls)
    tv.sync(_sets())                                                     # an API error: again at the next read ...
    assert len(api.calls) == n
    now[0] += stt_terms.ERROR_BACKOFF_S + 1
    tv.sync(_sets())
    assert len(api.calls) > n


def test_a_failed_vocabulary_is_tried_again_later_and_a_deleted_one_is_made_again():
    kv, api, now = {}, FakeApi(pending=0), [1000.0]
    tv = _tv(kv, api, clock=lambda: now[0])
    s1 = _sets()
    tv.want("county_fire", "")
    tv.sync(s1)
    assert wait(lambda: tv.ready("county_fire", ""))
    name = tv.ready("county_fire", "")
    del api.vocabs[name]                                                 # deleted out of band
    tv.sync(s1)
    assert name not in api.vocabs                                        # (looked at once an hour)
    now[0] += stt_terms.RECHECK_S + 1
    tv.sync(s1)
    assert wait(lambda: name in api.vocabs)
    # FAILED: never at once again; after the backoff, the same name again
    kv[f"aws_vocabulary_terms_county_fire_none"] = json.dumps({"state": "FAILED", "reason": "x"})
    n = len(api.calls)
    tv.sync(s1)
    assert len(api.calls) == n and tv.meta("county_fire", "")["fails"] == 1
    now[0] += stt_terms.FAILED_BACKOFF_S[0] + 1
    tv.sync(s1)
    assert any(op == "GetVocabulary" for op, _ in api.calls[n:])


def test_terms_aws_off_deletes_what_was_made():
    kv, api = {}, FakeApi(pending=0)
    tv = _tv(kv, api)
    tv.want("county_fire", "sizeup-pcfd")
    tv.sync(_sets())
    assert wait(lambda: tv.ready("county_fire", "sizeup-pcfd"))
    assert TermVocabularies("us-east-1", kv.get, kv.__setitem__, call=api, log=lambda m: None).cleanup() == 1
    assert not api.vocabs and tv.pairs() == []


def test_aws_phrases_are_written_with_digits_and_without_accents():
    assert aws_phrases(["Café Row", "ten four", "Engine one forty two", "日本"]) == ["Cafe Row", "10-4", "Engine 142"]


class _Job(SimpleNamespace):
    pass


def test_a_term_vocabulary_goes_ahead_of_the_channels_own_and_its_version_is_recorded(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    t = AwsTranscriber(Settings(), Usage(lambda k: None, lambda k, v: None), lambda *a: None, lambda *a: None,
                       log=lambda m: None, respell=lambda s: s.replace("sixty control", "60 Control"))
    name = "sizeup-terms-county_fire-pcfd-0123456789ab"
    t.vocabulary, t.channel_vocabulary["p25:mrrs:9211"] = "sizeup-pcfd", "sizeup-pd"
    assert t.vocab_for("FG1") == "sizeup-pcfd" and t.vocab_for("p25:mrrs:9211") == "sizeup-pd"
    t.term_vocabulary = lambda ch: name if ch == "FG1" else ""
    assert t.vocab_for("FG1") == name and t.vocab_for("p25:mrrs:9211") == "sizeup-pd"
    assert f"vocabulary-name={name}" in t.url("FG1")
    t.term_vocabulary = lambda ch: 1 / 0                                  # trouble there: the channel's own
    assert t.vocab_for("FG1") == "sizeup-pcfd"
    from sizeup.transcribe import Job
    got = []
    t.on_done = lambda item, text, conf: got.append((item.vocab_v, text))
    t.vocab_version = vocab_v_of
    t.vocab_respell = lambda n: {"engine one forty two": "Engine 142"} if n == name else {}
    job = Job("FG1", _Job(vocab_v=""), np.zeros(10, np.int16), vocab=name)
    t._open.add(job)
    t._done(job, "sixty control, engine one forty two", 0.9)            # the channel's respelling, then the list's
    assert got == [("county_fire+pcfd@0123456789ab", "60 Control, Engine 142")]


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
    assert seen[0].endswith("working fire, Quillbrook, Zephyrton. 77 Alpha 3.")   # the top last, then the talkers
    assert "sixty" not in seen[0] and "Engine 60" not in seen[0]           # (no callsign: MAX_NUMERIC_PROMPT_TERMS)
    row = app.cad.store.one("SELECT text, vocab_v FROM radio_traffic WHERE id = ?", ("307-t",))
    assert wait(lambda: (app.cad.store.one("SELECT vocab_v FROM radio_traffic WHERE id = ?", ("307-t",)) or {})
                .get("vocab_v") == ts.vocab_v), row
    # another system's police: its own list (and its own profile prompt, after it)
    pd = SimpleNamespace(source="PD", source_key="p25:mrrs:9211", service="police", unit="", start=None, vocab_v="")
    p = app._prompt(pd)
    assert "respond to, Zephyrton. Police dispatch." in p and pd.vocab_v == app.stt_terms.get("pd_dispatch").vocab_v
    short, full = app._base_prompt(pd, short=True), app._base_prompt(pd)
    assert p.endswith(short) and len(short) < len(full)
    from sizeup import stt_vocab                                             # the busiest streets kept, the rest dropped
    prof = stt_vocab.profile("p25:mrrs:9211")
    assert short == stt_vocab.police_prompt(dict(prof, prompt_streets=stt_terms.PROFILE_STREETS))
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
    assert app._vocab_version("sizeup-terms-ems-none-0123456789ab") == "ems+none@0123456789ab"


def test_the_config_takes_the_switches(tmp_path):
    from sizeup.config import ConfigError, load
    base = (tmp_path / "c.toml")
    base.write_text('[stt]\nterms_whisper = true\nterms_aws = true\nterms_dir = "~/vocab"\n')
    c = load(base)
    assert c.stt_terms_whisper and c.stt_terms_aws and c.stt_terms_dir.endswith("/vocab") and "~" not in c.stt_terms_dir
    base.write_text('[stt]\nterms_aws = "yes"\n')
    with pytest.raises(ConfigError):
        load(base)


def test_the_hub_makes_each_channels_term_vocabulary_on_its_own_vocabulary(tmp_path, monkeypatch):
    """FG1 keeps sizeup-pcfd's phrases (as Settings > Radios > Vocabulary has them) inside its term vocabulary; a context
    source with no vocabulary of its own gets the list alone; until READY each streams with its own."""
    from tests.fake_transcribe import FakeTranscribeApi
    monkeypatch.delenv("LITESTREAM_BUCKET", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    api = FakeTranscribeApi(pending_polls=0)
    try:
        app = _app(tmp_path, stt_terms_aws=True, stt_aws=True, stt_aws_api_endpoint=api.url,
                   stt_aws_endpoint="ws://127.0.0.1:9")
        app._kind["FG1"] = "traffic"
        app.stt_terms.refresh()
        assert app._own_vocab("FG1") == "sizeup-pcfd" and app._own_vocab("p25:mrrs:9570") == ""
        own = app._own_vocab_phrases("sizeup-pcfd")
        assert own == app.cad.settings.vocabulary() and own
        assert app.aws.vocab_for("FG1") == ""                              # (sizeup-pcfd not pushed here: none) ...
        assert wait(lambda: app.term_vocabs.ready("county_fire", "sizeup-pcfd"), 10)   # ... the leader made FG1's
        name = app.aws.vocab_for("FG1")
        assert name.startswith("sizeup-terms-county_fire-pcfd-") and app.aws.vocab_for("tg:307") == name
        from sizeup.transcribe import to_aws
        sent = api.vocabularies[name]["phrases"]
        firsts = [a for a in (to_aws(p)[0] for p in own) if a]
        assert sent[:5] == list(dict.fromkeys(x.lower() for x in firsts))[:5] or sent[:5] == firsts[:5]
        assert app._vocab_version(name).startswith("county_fire+pcfd@")
        assert app.aws.vocab_respell(name)                                  # numbers come back as digits
        assert app.aws.vocab_for("p25:mrrs:9570") == ""                    # (no ems list here: its own, none)
        assert app._own_vocab("p25:mrrs:9211") == "sizeup-pd"                # police dispatch keeps sizeup-pd's ...
        app.aws.vocab_for("p25:mrrs:9211")
        assert wait(lambda: app.term_vocabs.ready("pd_dispatch", "sizeup-pd"), 10)
        pd = api.vocabularies[app.term_vocabs.ready("pd_dispatch", "sizeup-pd")]["phrases"]
        police = [a for a in (to_aws(p)[0] for p in app.police_vocabulary()) if a]
        assert pd[:20] == police[:20] and "Zephyrton" in pd                 # ... first, then its list
    finally:
        api.close()


def test_airband_keeps_the_atc_vocabulary_as_it_was_last_pushed():
    """The airband term vocabulary starts from sizeup-atc's phrases as AWS last got them (#257's refresher pushes the
    operators seen when they change): it changes only when sizeup-atc does, never with every operator seen or gone."""
    import hashlib
    from sizeup.app import App
    from sizeup.transcribe import build_vocabulary
    sig_of = lambda ph: hashlib.sha1(json.dumps(build_vocabulary(ph)["phrases"]).encode()).hexdigest()[:12]
    pushed = ["Westchester Tower", "Examplejet"]
    now = {"phrases": list(pushed)}
    state = {"sig": sig_of(pushed)}
    kv = {}
    stub = SimpleNamespace(_kind={"HPN Tower": "airband"}, ATC_PUSHED=App.ATC_PUSHED,
                           atc_vocab=SimpleNamespace(name="sizeup-atc", state=lambda: dict(state)),
                           atc_refresh=SimpleNamespace(phrases=lambda: list(now["phrases"])), pd_vocab=None, pd_sources=(),
                           cad=SimpleNamespace(store=SimpleNamespace(meta_get=kv.get, meta_put=kv.__setitem__)),
                           _atc_leader=lambda: True, cfg=SimpleNamespace(stt_aws_vocabulary="sizeup-pcfd"))
    stub._atc_pushed_phrases = lambda: App._atc_pushed_phrases(stub)
    assert App._own_vocab(stub, "HPN Tower") == "sizeup-atc"
    assert App._own_vocab_phrases(stub, "sizeup-atc") == pushed                      # what AWS has: kept
    now["phrases"] = [*pushed, "Otherair"]                                          # an operator seen, not pushed yet
    assert App._own_vocab_phrases(stub, "sizeup-atc") == pushed                      # unchanged: no new vocabulary
    state["sig"] = sig_of(now["phrases"])                                           # the refresher pushed it
    assert App._own_vocab_phrases(stub, "sizeup-atc") == [*pushed, "Otherair"]
    now["phrases"] = list(pushed)                                                   # gone again, not pushed yet
    assert App._own_vocab_phrases(stub, "sizeup-atc") == [*pushed, "Otherair"]
    assert App._own_vocab_phrases(stub, "") == []


# -- the GPU workers (#651): the same no-speech rules, and stt_runs keeps vocab_v -------------------------------------


def test_a_gpu_workers_text_gets_the_read_back_guard_and_every_run_keeps_its_vocab_v(tmp_path):
    from sizeup import sttpool
    from sizeup.stt import URGENT, judge
    from tests.test_cloud import HubRun, hub_cfg
    p = "Zephyrton, working fire, second alarm, all hands, Quillbrook, Marrowind Avenue, Example Post Road."
    vv = "county_fire@0123456789ab"
    # judge: the guard only with a list in the prompt
    assert judge(p, 2.0, prompt=p, vocab_v=vv) == ("", "no_speech")
    assert judge(p, 2.0, prompt=p) == (p, "done")
    assert judge("Engine 61 on location, working fire.", 2.0, prompt=p, vocab_v=vv)[1] == "done"
    hub = HubRun(hub_cfg(tmp_path))
    try:
        app = hub.app
        for tid in ("FG1-71", "FG1-72", "FG1-73"):
            app.cad.comms.record({"id": tid, "start": 1.0, "end": 3.0, "source": "FG1", "talkgroup": None, "text": "",
                                  "status": "pending", "model": sttpool.PENDING_MODEL})

        def job(tid, vocab_v):
            return sttpool.Job(tid, URGENT, "FG1", "traffic", 2.0, audio=lambda: None, on_done=lambda *a: None,
                               on_fallback=lambda *a: None, prompt=p, extra={"vocab_v": vocab_v})

        app._pool_done(job("FG1-71", vv), {"text": p, "model": "large-v3-turbo-q5_0", "words": []})   # read back
        app._pool_done(job("FG1-72", vv), {"text": "Engine 61 on location, working fire.", "model": "m"})
        app._pool_done(job("FG1-73", ""), {"text": p, "model": "m"})                                   # no list: off
        q = lambda tid: app.cad.store.one("SELECT text, status, vocab_v FROM radio_traffic WHERE id = ?", (tid,))
        assert q("FG1-71")["status"] == "no_speech" and q("FG1-71")["text"] == ""
        assert q("FG1-72")["status"] == "done" and q("FG1-72")["vocab_v"] == vv
        assert q("FG1-73")["status"] == "done"
        assert wait(lambda: all(app.stt_runs.of(t) for t in ("FG1-71", "FG1-72", "FG1-73")), 5)
        (r1,), (r2,), (r3,) = (app.stt_runs.of(t) for t in ("FG1-71", "FG1-72", "FG1-73"))
        assert r1["text"] == p and r1["status"] == "no_speech" and r1["vocab_v"] == vv   # the raw words kept, judged
        assert r2["vocab_v"] == vv and r3["vocab_v"] == "" and r1["prompt_v"]
        # AWS's runs keep the vocabulary its stream used; the station's whisper's, what its event said
        from sizeup.app import AwsJob
        app._aws_done(AwsJob("traffic", "FG1-72", "FG1", rerun=True, vocab_v="county_fire+pcfd@0123456789ab"),
                      "words", 0.9)
        assert app.stt_runs.of("FG1-72")[-1]["vocab_v"] == "county_fire+pcfd@0123456789ab"
    finally:
        hub.close()


def test_a_runs_file_from_before_vocab_v_gets_the_column(tmp_path):
    import sqlite3
    from sizeup import sttruns
    path = tmp_path / "runs.sqlite"
    old = sttruns.SCHEMA.replace(", vocab_v TEXT NOT NULL DEFAULT ''", "")
    assert old != sttruns.SCHEMA
    db = sqlite3.connect(path)
    db.executescript(old)
    db.close()
    runs = sttruns.Runs(str(path), log=lambda m: None)
    assert runs.add("FG1-9", "aws", text="x", vocab_v="ems+none@0123456789ab")
    assert runs.of("FG1-9")[0]["vocab_v"] == "ems+none@0123456789ab"
