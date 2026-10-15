"""FG1 transmissions and radio IDs (GitHub #163): "these don't have radio ids and were added to this inc, they
should have ids and we should only add them if we know it, or ask if we see a new id"; "maybe it's a different
radio that is missing the ID?"; "we don't seem to do heard for fireground radios, we should follow the same
pattern as P25".

    attach     an FG1 transmission goes on an event only from a radio the registry names, on a unit on that event
               (sizeup/cad/comms.py); one with no ID stays in the Radio view; the old channel links are dropped
    no ID      why, per transmission: no ID sent / ID not decoded / too weak to tell (sizeup/app.py _id_note,
               radio/fm.py id_burst_frames, radio/tap.py burst), shown as a chip next to the channel, never a talker
    new ID     an unnamed ID heard on FG1 is asked about (sizeup/cad/newids.py); naming it links its transmissions
    heard      MDC and FleetSync IDs get registry rows, last-heard times and the channel, as P25's do
"""
import json
import re
import wave
from pathlib import Path

import numpy as np
import pytest

from sizeup.cad.api import CadApi, needs
from sizeup.cad.newids import ASK_S
from sizeup.radio import fleetsync
from sizeup.radio.fm import RATE, Channel, Segment, id_burst_frames
from sizeup.radio.mdc1200 import encode as mdc_encode
from sizeup.web.server import Broadcaster
from tests.test_radios import Clock, _event_with, cad, tx, uid

STATIC = Path(__file__).resolve().parent.parent / "sizeup" / "web" / "static"
FIXTURES = Path(__file__).parent / "fixtures"
rng = np.random.default_rng(163)


def post(c, path, body):
    code, raw, _ = CadApi(c).post(path, json.dumps(body).encode())
    return code, json.loads(raw)


def radio(c, iid):
    return {r["id"]: r["why"] for r in c.full(iid)["radio"]}


# -- the attach rule ---------------------------------------------------------------------------------------------


def test_fg1_with_no_id_stays_off_the_call_and_says_why(tmp_path):
    # The screenshot: 26-000016 (E60, E62, L31, E63 on it), FG1 key-ups with no radio ID were on it as "FG1 FG1".
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E60", "E62", "L31", "E63"], clock)
    clock.t += 60
    for k, note in enumerate(("none", "undecoded", "weak", "")):
        t = dict(tx(f"FG1-{k}", clock.t + k * 3, text="The one that drove slow."), id_note=note)
        assert c.record_traffic(t) == []                                   # one call with units: still not on it
    assert radio(c, a["id"]) == {}
    rows = {r["id"]: r for r in c.comms_query({"channel": ["FG1"]})["rows"]}
    assert {k: (r["id_note"], r["events"], r["talker"], r["rig"]) for k, r in rows.items()} == {
        "FG1-0": ("none", [], "", ""), "FG1-1": ("undecoded", [], "", ""), "FG1-2": ("weak", [], "", ""),
        "FG1-3": ("", [], "", "")}
    # an ID'd radio of a unit on it: on it (the only way in); a note never sticks to a row that has an ID
    assert c.record_traffic(dict(tx("FG1-9", clock.t + 20, rig="E63", radio_id="6301"), id_note="none")) == [a["id"]]
    assert radio(c, a["id"]) == {"FG1-9": "unit"}
    assert next(r for r in c.comms_query({})["rows"] if r["id"] == "FG1-9")["id_note"] == ""
    # junk in id_note is not kept
    c.record_traffic(dict(tx("FG1-x", clock.t + 30), id_note="<b>"))
    assert next(r for r in c.comms_query({})["rows"] if r["id"] == "FG1-x")["id_note"] == ""


def test_airport_talkgroup_links_only_an_identified_talker(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    ap = c.create({"type": "AIRPORT", "address": "Westchester County Airport"})
    clock.t += 5
    assert c.record_traffic(tx("333-1-call_1", clock.t, "Airport Incident", "", "", 333)) == []          # no ID, no alias
    assert c.record_traffic(tx("333-1-call_2", clock.t + 1, "Airport Incident", "", "AP7", 333, radio_id="5501")) == [ap["id"]]


# -- the new-ID prompt -------------------------------------------------------------------------------------------


def test_a_new_fg1_id_is_asked_about_and_naming_it_links_its_transmissions(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E60", "E62", "L31", "E63"], clock)
    clock.t += 30
    for k in range(3):                                   # 7F21: decoded, in no registry row, three key-ups
        c.radios.heard("mdc", "7f21", at=clock.t + k, channel="FG1")
        assert c.record_traffic(tx(f"FG1-n{k}", clock.t + k, unit="7F21", radio_id="7F21")) == []
    c.radios.heard("fleetsync", "167-1002", at=clock.t + 5, channel="FG1")
    c.record_traffic(tx("FG1-fs", clock.t + 5, unit="167-1002", radio_id="167-1002"))
    c.record_traffic(tx("FG1-k", clock.t + 6, rig="E63", radio_id="6301"))           # a named one: not asked
    clock.t += 10
    asked = c.state()["radio_ids"]
    assert [(x["system"], x["radio_id"], x["kind"], x["channel"], x["count"]) for x in asked] == [
        ("fleetsync", "167-1002", "FleetSync", "FG1", 1), ("mdc", "7F21", "MDC", "FG1", 3)]
    q = asked[1]
    assert q["traffic_id"] == "FG1-n2" and q["first_at"] < q["last_at"]
    assert [(i["number"], i["units"]) for i in q["incidents"]] == [(a["number"], ["E60", "E62", "E63", "L31"])]
    # the registry: a "heard" row, as P25's are
    row = c.radios.get("mdc", "7F21")
    assert (row["source"], row["unit"], row["last_channel"], row["edited"]) == ("heard", "", "FG1", 0)
    # named E62 on the console: the row is E62's (confirmed, in the radios log), its transmissions go on the call
    code, r = post(c, "/api/cad/radio-id", {"system": "mdc", "radio_id": "7f21", "unit": "e62", "label": "Portable 2",
                                            "operator": "Lt. Ruiz"})
    assert code == 200 and r["linked"] == [a["number"]] and r["radio"]["unit"] == "E62"
    assert (r["radio"]["source"], r["radio"]["label"], r["radio"]["edited"]) == ("confirmed", "Portable 2", 1)
    assert radio(c, a["id"]) == {"FG1-n0": "unit", "FG1-n1": "unit", "FG1-n2": "unit", "FG1-k": "unit"}
    assert {r["id"]: r["rig"] for r in c.comms_query({"unit": ["E62"]})["rows"]} == {"FG1-n0": "E62", "FG1-n1": "E62",
                                                                                    "FG1-n2": "E62"}
    assert any(x["key"] == "mdc:7F21/unit" and x["new"] == "E62" and x["operator"] == "Lt. Ruiz" for x in c.radios.log())
    assert [x["radio_id"] for x in c.state()["radio_ids"]] == ["167-1002"]
    # from now on, its new transmissions go on the call by themselves
    assert c.record_traffic(tx("FG1-n3", clock.t + 1, rig=c.radios.rig_mdc(["7F21"]), radio_id="7F21")) == [a["id"]]
    # already named: not answered again here (Settings > Radios); ignore stops the question, links nothing
    assert post(c, "/api/cad/radio-id", {"system": "mdc", "radio_id": "7F21", "unit": "E60"})[0] == 400
    code, r = post(c, "/api/cad/radio-id", {"system": "fleetsync", "radio_id": "167-1002", "ignore": True})
    assert code == 200 and r["linked"] == [] and r["radio"]["unit"] == "" and r["radio"]["edited"] == 1
    assert c.state()["radio_ids"] == []
    c.radios.heard("fleetsync", "167-1002", at=clock.t + 2, channel="FG1")
    c.record_traffic(tx("FG1-fs2", clock.t + 2, unit="167-1002", radio_id="167-1002"))
    assert c.state()["radio_ids"] == [] and c.radios.resolve("fleetsync", "167-1002") is None
    # what isn't an answer
    for bad in ({"system": "p25", "radio_id": "41228", "unit": "E60"}, {"system": "mdc", "radio_id": "1234"},
                {"system": "mdc", "radio_id": "1234", "unit": "E60", "ignore": True},
                {"system": "mdc", "radio_id": "1234", "unit": "Q99"}, {"system": "mdc", "radio_id": "nope", "label": "x"}):
        assert post(c, "/api/cad/radio-id", bad)[0] == 400, bad


def test_a_name_with_no_unit_is_kept_and_links_nothing_and_old_questions_go(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["E60"], clock)
    clock.t += 5
    c.radios.heard("mdc", "7E01", at=clock.t, channel="FG1")
    c.record_traffic(tx("FG1-1", clock.t, unit="7E01", radio_id="7E01"))
    assert [x["radio_id"] for x in c.state()["radio_ids"]] == ["7E01"]
    clock.t += ASK_S + 61                                                          # heard over half an hour ago
    assert c.state()["radio_ids"] == []
    clock.t -= ASK_S + 61
    code, r = post(c, "/api/cad/radio-id", {"system": "mdc", "radio_id": "7E01", "label": "Rye PD car"})
    assert code == 200 and r["linked"] == [] and (r["radio"]["unit"], r["radio"]["label"]) == ("", "Rye PD car")
    assert radio(c, a["id"]) == {} and c.state()["radio_ids"] == []


def test_naming_a_radio_in_settings_takes_its_transmissions_too(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    a = _event_with(c, ["L31"], clock)
    clock.t += 5
    c.record_traffic(tx("FG1-1", clock.t, unit="3B07", radio_id="3B07"))
    c.record_traffic(tx("FG1-2", clock.t + 1, unit="3B07", radio_id="3B07"))
    c.record_traffic(tx("FG1-3", clock.t + 2, rig="E63", unit="3B07, 6301", radio_id="3B07, 6301"))   # named already: kept
    c.save_radio({"system": "mdc", "radio_id": "3B07", "unit": "L31", "operator": "Corey"})
    assert radio(c, a["id"]) == {"FG1-1": "unit", "FG1-2": "unit"}
    # a person took one off the call: naming the radio again doesn't put it back
    c.unlink_traffic(a["id"], {"traffic_id": "FG1-2", "operator": "Corey"})
    with c.store.tx() as db:
        db.execute("UPDATE radio_traffic SET rig = '' WHERE id = 'FG1-2'")
    c.comms.relink("mdc", "3B07", "L31")
    assert radio(c, a["id"]) == {"FG1-1": "unit"}


def test_who_may_answer(tmp_path):
    assert needs("/api/cad/radio-id") == "dispatch"
    scripts = (STATIC / "radio-ids.js").read_text()
    assert '"/api/cad/radio-id"' in scripts and 'canDo("dispatch")' in scripts
    html = (STATIC / "cad.html").read_text()
    assert '<script src="/static/radio-ids.js"></script>' in html and 'id="q-newids"' in html


def test_the_question_sits_with_the_units_and_the_radio():
    # #177: in the right column, under the radio (the 60 Control feed) and above the units; not in the incident queue
    html = (STATIC / "cad.html").read_text()
    queue = html[html.index('<aside class="queue">'):html.index("</aside>")]
    side = html[html.index('<aside class="units">'):]
    side = side[:side.index("</aside>")]
    assert 'id="newids"' not in queue and "q-newids" not in queue
    assert side.index('id="feed"') < side.index('id="newids"') < side.index('id="q-newids"') < side.index('id="units"')
    assert '<section id="newids" class="nid-sec console-only" hidden>' in side      # hidden until there is a question


def test_the_question_does_not_churn_the_push(tmp_path):
    # #146: only changed sections go out. The questions change when something is heard or answered, not by the second.
    clock = Clock()
    c = cad(tmp_path, clock)
    _event_with(c, ["E60"], clock)
    clock.t += 5
    c.radios.heard("mdc", "7D01", at=clock.t, channel="FG1")
    c.record_traffic(tx("FG1-1", clock.t, unit="7D01", radio_id="7D01"))
    b = Broadcaster(lambda: {})
    s1 = b.publish(c.state())
    for _ in range(3):
        clock.t += 1
        c.radios.heard("mdc", "7D01", at=clock.t, channel="FG1")      # heard again (a last-heard time): no section moves
        s = b.publish(c.state())
    assert "radio_ids" not in json.loads(b.body(b.snapshot(), s1)) and s > s1
    c.record_traffic(tx("FG1-2", clock.t, unit="7D01", radio_id="7D01"))
    s2 = b.publish(c.state())
    assert json.loads(b.body(b.snapshot(), s))["radio_ids"][0]["count"] == 2
    clock.t += 1
    b.publish(c.state())
    assert "radio_ids" not in json.loads(b.body(b.snapshot(), s2))


# -- heard, as P25 -----------------------------------------------------------------------------------------------


def test_fg1_ids_are_heard_like_p25_at_the_same_write_rate(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    reg = c.radios
    reg.heard("p25", "41228", "E60", at=clock.t, channel="Fire/EMS 15")
    p = reg.get("p25", "41228")
    assert (p["source"], p["unit"], p["last_heard"], p["last_channel"]) == ("heard", "E60", clock.t, "Fire/EMS 15")
    g = reg.get("mdc", "6301")
    assert g["source"] in ("guessed", "confirmed") and g["unit"] == "E63"
    rev = reg.rev
    reg.heard("mdc", "6301", at=clock.t, channel="FG1")                # a seeded row: keeps its source, gets heard
    g = reg.get("mdc", "6301")
    assert (g["source"], g["unit"], g["last_heard"], g["last_channel"], g["heard"]) == (g["source"], "E63", clock.t, "FG1", 1)
    db = lambda: c.store.one("SELECT last_heard, heard FROM radios WHERE system = 'mdc' AND radio_id = '6301'")
    assert db() == {"last_heard": clock.t, "heard": 1}
    reg.heard("mdc", "6301", at=clock.t + 30, channel="FG1")           # within a minute: the cache, not the disk
    assert reg.get("mdc", "6301")["last_heard"] == clock.t + 30 and db() == {"last_heard": clock.t, "heard": 1}
    reg.heard("mdc", "6301", at=clock.t + 61, channel="FG1")
    assert db() == {"last_heard": clock.t + 61, "heard": 2}
    assert reg.rev == rev                                              # a last-heard time is not a registry change
    reg.heard("fleetsync", "167-1002", at=clock.t + 62, channel="FG1")
    fs = reg.get("fleetsync", "167-1002")
    assert (fs["source"], fs["unit"], fs["last_channel"]) == ("heard", "", "FG1") and reg.rev == rev + 1
    # the list: FG1's rows shown by default, heard ones first in their system, the channel in its own column
    js = (STATIC / "radios-panel.js").read_text()
    assert '!FM.has(r.system)' in js and '"Heard on"' in js and "r.last_channel" in js and "d.id_stats" in js
    assert c.radio_registry()["id_stats"] == []


def test_id_stats_count_fg1_key_ups_by_why(tmp_path):
    clock = Clock()
    c = cad(tmp_path, clock)
    for k, (note, rid) in enumerate((("none", ""), ("none", ""), ("undecoded", ""), ("weak", ""), ("", ""), ("", "6301"))):
        c.record_traffic(dict(tx(f"FG1-{k}", clock.t + k, radio_id=rid, unit=rid), id_note=note))
    c.record_traffic(tx("307-1-call_1", clock.t, "Fire/EMS 15", "", "", 307))
    assert c.radio_registry()["id_stats"] == [{"channel": "FG1", "n": 6, "with_id": 1, "none": 2, "undecoded": 1,
                                               "weak": 1, "unknown": 1}]


# -- why there is no ID ------------------------------------------------------------------------------------------


def q(s):
    return 0.003 * rng.standard_normal(int(RATE * s))


def pure(hz, s):
    return 0.5 * np.sin(2 * np.pi * hz * np.arange(int(RATE * s)) / RATE)


def read_wav(p):
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float64) / 32768


@pytest.mark.parametrize("burst", [mdc_encode(0x01, 0x80, 0x6301), mdc_encode(0x01, 0x00, 0x0611),
                                   fleetsync.encode(167, 1002), fleetsync.encode(167, 1002, baud=2400)],
                         ids=["mdc-pre", "mdc-post", "fleetsync-1200", "fleetsync-2400"])
def test_an_id_burst_is_seen_as_one(burst):
    assert any(id_burst_frames(np.concatenate([q(0.3), burst, q(0.3)])))


@pytest.mark.parametrize("path", sorted((FIXTURES / "mdc").glob("*.wav")) + sorted((FIXTURES / "fg1").glob("*.mdc.wav")),
                         ids=lambda p: p.name)
def test_real_fg1_bursts_are_seen(path):
    assert any(id_burst_frames(read_wav(path)))


def yelp(s):
    t = np.arange(int(RATE * s)) / RATE
    return 0.5 * np.sin(2 * np.pi * np.cumsum(1050 + 450 * np.sin(2 * np.pi * 3.5 * t)) / RATE)


@pytest.mark.parametrize("name,audio", [
    ("roger-beep-1000", np.concatenate([q(0.3), pure(1000, 0.2), q(0.3)])),
    ("talk-permit-1800", np.concatenate([q(0.3), pure(1800, 0.2), q(0.3)])),
    ("dtmf", np.concatenate([q(0.3), pure(770, 0.2) + pure(1336, 0.2), q(0.3)])),
    ("siren-yelp", np.concatenate([q(0.3), yelp(3.0), q(0.3)])),
    ("hiss", 0.4 * rng.standard_normal(2 * RATE)),
])
def test_tones_sirens_and_hiss_are_not_bursts(name, audio):
    assert not any(id_burst_frames(audio)), name


def test_real_fg1_voice_is_not_a_burst():
    assert not any(id_burst_frames(read_wav(FIXTURES / "fg1" / "FG1-1791491541708.src.wav")))


def wiped(burst, first_bit, nbits):
    a, n = int(first_bit * RATE / 1200), int(nbits * RATE / 1200)
    out = burst.copy()
    out[a: a + n] = 0.5 * np.sin(2 * np.pi * 1500 * np.arange(n) / RATE)
    return out


@pytest.fixture
def app(tmp_path):
    from sizeup.app import App
    from tests.test_app import cfg
    a = App(cfg(tmp_path))
    a._kind = {"FG1": "traffic"}
    return a


def test_why_no_id(app):
    from tests.test_app import voice
    v = voice(3.0)
    seg = lambda q_db, bursts=(), audio=v: Segment(1000.0, 1003.0, audio, q_db, list(bursts))
    assert app._id_note("FG1", seg(35.0), ["6301"]) == ""                                     # it has one
    assert app._id_note("FG1", seg(35.0), []) == "none"                                       # strong, no burst at all
    assert app._id_note("FG1", seg(12.0), []) == "weak"                                       # weak: can't tell
    broken = wiped(mdc_encode(0x01, 0x00, 0x6301), 56 + 40 + 10, 90).astype(np.float32)     # a post-ID that won't decode
    assert app._id_note("FG1", seg(35.0, [broken]), []) == "undecoded"
    assert app._id_note("FG1", seg(12.0, [broken]), []) == "undecoded"                        # weak, but a burst was there
    beep = np.concatenate([v, pure(1800, 0.2).astype(np.float32)])                            # a talk-permit / roger beep
    assert app._id_note("FG1", Segment(1000.0, 1003.2, beep, 35.0), []) == "none"
    fs2 = fleetsync.encode(167, 1002, baud=2400, reversals=60)                                # FleetSync II at release:
    fs2[-int(0.012 * RATE):] = 0                                                               # its block cut short
    assert fleetsync.decode(fs2, RATE) == []
    tail = np.concatenate([v, fs2, q(0.05)]).astype(np.float32)                                # left in the audio (the
    assert app._id_note("FG1", Segment(1000.0, 1003.1, tail, 35.0), []) == "undecoded"         # squelch cuts 1200/1800 only)
    mid = np.concatenate([v[:RATE], fs2, v[RATE:]]).astype(np.float32)                         # mid-transmission: not counted
    assert app._id_note("FG1", Segment(1000.0, 1003.1, mid, 35.0), []) == "none"


def test_a_key_up_burst_the_tap_saw_but_could_not_decode():
    from tests.test_livetap import Rig, hiss, keyed, voice as tap_voice
    r = Rig()
    broken = wiped(mdc_encode(0x01, 0x80, 0x6301), 56 + 40 + 10, 90)
    r.feed(np.concatenate([hiss(3.0), keyed(np.concatenate([q(0.1), broken, tap_voice(2.0)])), hiss(1.0)]))
    seg = r.segs[0]
    assert r.tap.ids("FG1", seg.start, seg.end) == [] and r.tap.burst("FG1", seg.start, seg.end)
    r2 = Rig()                                                    # a short key-up (under the 1.5 s window)
    r2.feed(np.concatenate([hiss(3.0), keyed(np.concatenate([q(0.1), broken, q(0.5)])), hiss(1.0)]))
    assert r2.tap.burst("FG1", r2.segs[0].start, r2.segs[0].end)
    r3 = Rig()                                                    # no burst: nothing
    r3.feed(np.concatenate([hiss(3.0), keyed(tap_voice(2.0)), hiss(1.0)]))
    assert not r3.tap.burst("FG1", r3.segs[0].start, r3.segs[0].end)


def test_an_fg1_key_up_reaches_the_cad_with_why_and_its_id_is_heard(app):
    from tests.test_app import FakeWhisper, voice, wait
    app.stt.client = FakeWhisper({3: "I don't know if that can go any faster"})
    app.stt.start()
    ch = Channel(453.0375, "FG1")
    app._segment(ch, Segment(1_791_400_000.0, 1_791_400_003.0, voice(3.0), 35.0))
    app._segment(ch, Segment(1_791_400_010.0, 1_791_400_013.0, voice(3.0), 35.0,
                             [mdc_encode(0x01, 0x00, 0x7C01).astype(np.float32)]))
    assert wait(lambda: len([r for r in app.cad.comms.query(channel="FG1")]) == 2)
    rows = {r["start"]: r for r in app.cad.comms.query(channel="FG1")}
    assert (rows[1_791_400_000.0]["id_note"], rows[1_791_400_000.0]["radio_id"]) == ("none", "")
    assert (rows[1_791_400_010.0]["id_note"], rows[1_791_400_010.0]["radio_id"]) == ("", "7C01")
    heard = app.radios.get("mdc", "7C01")
    assert (heard["source"], heard["last_channel"], heard["last_heard"]) == ("heard", "FG1", 1_791_400_013.0)


def test_the_row_shows_the_channel_once_and_why_there_is_no_id():
    js = (STATIC / "cad.js").read_text()
    assert "channel: \"FG1\"" not in js and "only call with units" not in js           # no FG1 "why" chip any more
    body = re.search(r"^function radioRow\(t, ctx = \{\}\) \{(.*?)^\}", js, re.S | re.M).group(1)
    assert 'meta.append(el("span", "chip src", t.channel));' in body and "noIdChip(t)" in body
    assert body.count("t.channel") == 1
    for k in ("No ID sent", "ID not decoded"):
        assert k in js
