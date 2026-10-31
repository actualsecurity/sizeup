"""#47: our tones as heard on the air (60 Control tone tests 10-20 and 10-23-2026, 18 days of stored South 2 pages):
Rye Brook Fire 767.4/928.1 and Port Chester Fire 903.2/810.2, both ours (PCFD answers both villages), often stacked on
one page; Port Chester-Rye-Rye Brook EMS 928.1/510.5 is not ours; the RadioReference wiki's 349.1/311.1 was never heard.
A default install, the hub (Settings > Radios > Tones on the list), a station (its own [[tones]], fixed once on update
by sizeup/cloud/configupdate.py), the dispatch tones, and the page tags (one tag per pair, its own tones in it)."""

import json
import os
import shutil
import subprocess
import time
import tomllib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sizeup.app import App
from sizeup.cad import tonelist
from sizeup.cloud import configupdate
from sizeup.config import load
from sizeup.radio.fm import RATE, Channel, Segment
from sizeup.radio.tones import base_name
from tests.test_app import FakeWhisper, cfg, tone, voice, wait
from tests.test_fg2 import installed
from tests.test_tone_settings import page_row

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "sizeup" / "web" / "static"
EXAMPLE = load(ROOT / "config.example.toml")
RB, PC, EMS, WIKI = (767.4, 928.1), (903.2, 810.2), (928.1, 510.5), (349.1, 311.1)
CASES = {"rye brook alone": ([RB], True), "port chester alone": ([PC], True), "stacked": ([RB, PC], True),
         "ems alone": ([EMS], False)}


def default_app(tmp_path, **kw):
    """An install from config.example.toml: the county list + its [[tones]], its ours and monitor_departments."""
    return App(replace(cfg(tmp_path), tones=EXAMPLE.tones, ours=EXAMPLE.ours,
                       cad_monitor_departments=EXAMPLE.cad_monitor_departments, **kw))


def tset(app, **body):
    return app.cad.settings.post("tones", dict(body, action=body.pop("action", "set"), operator="Dana"))


# -- a default install: the list and the config say whose pairs these are -------------------------------------------

def test_the_defaults_name_both_pairs_ours_and_the_ems_pair_not():
    assert EXAMPLE.ours == {"Port Chester FD", "Rye Brook FD"}
    by_name = {t.name: (t.a_hz, t.b_hz) for t in EXAMPLE.tones if t.source == "config"}
    assert by_name == {"Port Chester FD": PC, "Rye Brook FD": RB}                  # [[tones]]: no 349.1/311.1
    assert not any((t.a_hz, t.b_hz) == WIKI for t in EXAMPLE.tones)                # nor in the list (never heard)
    assert EXAMPLE.dispatch_tones == ((903.2, 1.0), (810.2, 3.0))


@pytest.mark.parametrize("case", list(CASES))
def test_a_page_on_our_pairs_is_ours_and_the_ems_pair_is_not(tmp_path, case):
    pairs, ours = CASES[case]
    app = default_app(tmp_path)
    names = [app.plan.match_pair(a, b) for a, b in pairs]
    assert all(len(n) == 1 for n in names) or case == "ems alone"                   # one name a pair, no conflict
    assert app.paging.is_ours([n for ns in names for n in ns]) is ours
    assert app.store.is_ours([n for ns in names for n in ns]) is ours
    page_row(app, "p", time.time() - 60, pairs, "Engine 61, 209 Westchester Avenue")
    row = next(r for r in app.cad.feed.rows(limit=5) if r["id"] == "p")
    assert row["page"] == ("ours" if ours else "unknown")
    assert [t["ours"] for t in row["tones"]] == [ours] * len(pairs)                 # each pair's own flag
    if case == "stacked":                                                           # each pair keeps its own name
        assert [[base_name(n) for n in t["names"]] for t in row["tones"]] == [["Rye Brook · Fire"], ["Port Chester · Fire"]]


def test_the_book_puts_each_pair_under_its_village_and_the_hubs_overrides_agree(tmp_path):
    app = default_app(tmp_path)
    book = app.cad.settings.tone_book()
    rb, pc = book.find("Rye Brook FD"), book.find("Port Chester FD")
    assert (rb.name, rb.ours, rb.ours_locked) == ("Rye Brook · Fire", True, True)
    assert (pc.name, pc.ours, pc.ours_locked) == ("Port Chester · Fire", True, True)
    assert {(p.a_hz, p.b_hz) for p in rb.pairs if p.state != "deleted"} == {RB}
    assert {(p.a_hz, p.b_hz) for p in pc.pairs if p.state != "deleted"} == {PC}
    assert not any(p.conflicts for e in (rb, pc) for p in e.pairs)
    # The live hub's entry (903.2/810.2 = Port Chester · Fire) and the one the user adds for Rye Brook: the same
    # entities as the defaults, so no conflict and still one name a pair, ours.
    tset(app, a_hz=903.2, b_hz=810.2, places=["Port Chester"], service="fire")
    tset(app, a_hz=767.4, b_hz=928.1, places=["Rye Brook"], service="fire")
    book = app.cad.settings.tone_book()
    assert not any(p.conflicts for e in (book.find("Rye Brook FD"), book.find("Port Chester FD")) for p in e.pairs)
    assert [base_name(n) for n in app.plan.match_pair(*PC)] == ["Port Chester · Fire"]
    assert [base_name(n) for n in app.plan.match_pair(*RB)] == ["Rye Brook · Fire"]
    assert app.paging.is_ours(app.plan.match_pair(*PC)) and app.paging.is_ours(app.plan.match_pair(*RB))
    # Port Chester-Rye-Rye Brook EMS named as on the hub: named, still not ours (not PCFD)
    tset(app, a_hz=928.1, b_hz=510.5, places=["Port Chester", "Rye", "Rye Brook"], service="ems")
    assert app.plan.match_pair(*EMS) == ("Port Chester · Rye · Rye Brook · EMS",)
    assert not app.paging.is_ours(app.plan.match_pair(*EMS))
    page_row(app, "ems", time.time() - 30, [EMS])
    assert next(r for r in app.cad.feed.rows(limit=5) if r["id"] == "ems")["page"] == "other"


@pytest.mark.parametrize("case", ["rye brook alone", "stacked"])
def test_off_the_air_a_rye_brook_page_is_queued(tmp_path, case):
    """10-22 06:06 "Rye Brook, your Engine requested, Hutch northbound exit 17": on its own pair it wasn't ours."""
    pairs = CASES[case][0]
    app = default_app(tmp_path)
    app._kind = {"South 2": "paging"}
    app.stt.client = FakeWhisper({6: "Rye Brook, your engine requested, 900 King Street, MVA"})
    app.stt.start()
    audio = np.concatenate([*(np.concatenate([tone(a, 1.0), tone(b, 3.0)]) for a, b in pairs),
                            np.zeros(int(0.2 * RATE)), voice(6.0)]).astype(np.float32)
    now = time.time()
    app._segment(Channel(470.2, "South 2"), Segment(now, now + len(audio) / RATE, audio, 25.0))
    [page] = list(app.pages)
    assert [(round(t.a_hz), round(t.b_hz)) for t in page.tones] == [(round(a), round(b)) for a, b in pairs]
    assert wait(lambda: len(app.cad.store.incidents()) == 1)                        # ours: queued


# -- the station: its own [[tones]] say what's ours on its live paging display -------------------------------------

def old_station(text: str) -> str:
    """The station template before #47 (what an installed /etc/sizeup/config.toml has)."""
    start = text.index("# Our two-tone codes")
    end = text.index("[stt]")
    return text[:start] + ('[[tones]]\nname = "Port Chester FD"\n'
                           'a_hz = 349.1                       # RadioReference wiki (2025-07), unverified\n'
                           'b_hz = 311.1\nagency = "PCFD"\nours = true\n\n') + text[end:]


def station_ours(c) -> tonelist.Book:
    return tonelist.Book(c.tones, {}, set(c.ours) | set(c.cad_monitor_departments))


def test_an_installed_station_gets_the_heard_tones_once(tmp_path, monkeypatch):
    template = (ROOT / "deploy" / "station" / "config.station.toml").read_text()
    conf = installed(tmp_path, monkeypatch, old_station(template))
    marks = tmp_path / "etc" / "config-updates"
    before = load(conf)
    # Both names are ours whatever the file says (sizeup/config.py), and the release's list has both heard pairs, so
    # a station already knows them; what was wrong there is the wiki's pair, still a Port Chester one: a decode of it
    # would show as ours on the station's live paging (sent to the hub as it is) and its page, not on the hub.
    old = station_ours(before)
    assert old.find("Rye Brook FD").ours and old.find("Port Chester FD").ours
    assert {(p.a_hz, p.b_hz) for p in old.find("Port Chester FD").pairs} == {PC, WIKI}
    kept = conf.read_text()
    said = configupdate.run(conf, marks)
    assert said[0].startswith("fg2: already has")
    assert said[1].startswith("tones-47: Port Chester's 349.1/311.1 is now 903.2/810.2; Rye Brook FD 767.4/928.1 "
                              "(ours) added after it (the old file: ")
    assert conf.with_name("config.toml.before-tones-47").read_text() == kept        # the backup
    assert marks.read_text() == "fg2\ntones-47\n"
    after = load(conf)
    assert {t.name: (t.a_hz, t.b_hz) for t in after.tones if t.source == "config"} == {"Port Chester FD": PC,
                                                                                      "Rye Brook FD": RB}
    assert after.ours == {"Port Chester FD", "Rye Brook FD"}
    assert replace(after, tones=before.tones, ours=before.ours) == before           # nothing else changed
    book = station_ours(after)
    assert book.find("Rye Brook FD").ours and book.find("Port Chester FD").ours
    assert {(p.a_hz, p.b_hz) for p in book.find("Port Chester FD").pairs} == {PC}       # the wiki's pair is gone
    assert {(p.a_hz, p.b_hz) for p in book.find("Rye Brook FD").pairs} == {RB}
    assert not any(p.conflicts for e in (book.find("Rye Brook FD"), book.find("Port Chester FD")) for p in e.pairs)
    text = conf.read_text()
    assert text.replace(configupdate.RB_BLOCK, "\n", 1).count("[[tones]]") == 1      # added after Port Chester's
    assert "349.1" in text and "a_hz = 349.1" not in text                           # the comment says what it was
    # once: put back by hand, it stays as it was put
    conf.write_text(kept)
    assert configupdate.run(conf, marks) == [] and conf.read_text() == kept


def test_tones_set_by_hand_are_left_alone():
    new = (ROOT / "deploy" / "station" / "config.station.toml").read_text()            # a new install: already right
    assert configupdate.fix_tones(new) == (new, "tones-47: no [[tones]] entry on the unverified 349.1/311.1: nothing changed")
    other = '[[tones]]\nname = "Port Chester FD"\na_hz = 349.2\nb_hz = 311.1\nours = true\n'   # not exactly the old pair
    assert configupdate.fix_tones(other)[0] == other
    both = old_station(new).replace("[stt]", '[[tones]]\nname = "PC"\na_hz = 903.2\nb_hz = 810.2\n\n[stt]')
    assert configupdate.fix_tones(both) == (both, "tones-47: has 903.2/810.2 next to 349.1/311.1 (set by hand?): "
                                                  "nothing changed")
    with pytest.raises(ValueError, match="two"):
        configupdate.fix_tones(old_station(new) + '\n[[tones]]\nname = "X"\na_hz = 349.1\nb_hz = 311.1\n')
    # Rye Brook already there (by hand): only Port Chester's pair is fixed
    rb = old_station(new).replace("[stt]", '[[tones]]\nname = "Rye Brook FD"\na_hz = 767.4\nb_hz = 928.1\n\n[stt]')
    text, said = configupdate.fix_tones(rb)
    assert said == "tones-47: Port Chester's 349.1/311.1 is now 903.2/810.2; Rye Brook was already there"
    assert [(t["a_hz"], t["b_hz"]) for t in tomllib.loads(text)["tones"]] == [PC, RB]
    # a value written so the line can't be edited safely (349.10): nothing written, tried again next time
    with pytest.raises(ValueError, match="by hand"):
        configupdate.fix_tones(old_station(new).replace("a_hz = 349.1 ", "a_hz = 349.10"))


# -- the dispatch tones -------------------------------------------------------------------------------------------

def test_dispatch_tones_are_the_heard_pairs():
    hub = tomllib.loads((ROOT / "deploy" / "hub" / "config.hub.toml").read_text())
    ex = tomllib.loads((ROOT / "config.example.toml").read_text())
    assert hub["dispatch"]["tones"] == ex["dispatch"]["tones"] == [[903.2, 1.0], [810.2, 3.0]]
    assert hub["dispatch"]["transmit"] is False and ex["dispatch"].get("transmit", False) is False   # stays off
    lists = json.loads((ROOT / "data" / "cad_lists.json").read_text())
    assert lists["dispatch_tones"] == {"Rye Brook": [[767.4, 1.0], [928.1, 3.0]]}


# -- the page tags: each name with its own pair (the 10-15 13:57 screenshot) ---------------------------------------

NODE = shutil.which("node") or shutil.which("node", path="/opt/homebrew/bin:/usr/local/bin")

DOM = r"""
const fs = require("fs");
class N {
  constructor(tag) { this.tag = tag; this.kids = []; this.className = ""; this.t = ""; this.title = ""; this.hidden = false; }
  set textContent(v) { this.t = String(v); this.kids = []; }
  get textContent() { return this.t + this.kids.map((k) => k.textContent).join(""); }
  append(...xs) { for (const x of xs) this.kids.push(typeof x === "string" ? Object.assign(new N("#"), { t: x }) : x); }
  replaceChildren(...xs) { this.kids = []; this.append(...xs); }
  setAttribute() {} addEventListener() {}
}
global.window = global;
global.document = { createElement: (t) => new N(t), getElementById: () => null };
global.Audio = class { play() { return Promise.resolve(); } };
global.localStorage = { getItem: () => null, setItem: () => {} };
eval(fs.readFileSync(process.argv[2], "utf8"));
const IN = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const tag = (t) => ({ cls: t.className, parts: t.kids.map((k) => k.textContent), title: t.title });
const out = { tags: IN.pages.map((p) => Radio.pageTags(p).map(tag)) };
const box = new N("div");
Radio.fillPaging(box, IN.live, null);
out.live = box.kids.map((r) => r.title);
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_each_page_tag_holds_its_own_pair(tmp_path):
    """10-15 13:57: a stacked page, Rye Brook's pair not yet named, showed "767.4/928.1" beside "Port Chester · Fire" as
    if they were one. The pairs were right (each pair carries its own names); the tag for an unknown pair was its
    bare tones and a named tag had none, so the tones read as the next name's. Each tag now holds its pair's tones."""
    rb = {"a_hz": 767.4, "b_hz": 928.1, "names": ["Rye Brook · Fire (heard)"], "ours": True}
    rb_unknown = {"a_hz": 767.4, "b_hz": 928.1, "names": [], "ours": False}
    pc = {"a_hz": 903.2, "b_hz": 810.2, "names": ["Port Chester · Fire"], "ours": True}
    ems = {"a": 928.1, "b": 510.5, "names": ["Port Chester · Rye · Rye Brook · EMS"], "ours": False}
    live = [{"key": "k", "channel": "South 2", "phase": "tones", "ours": True, "ended": False,
             "pairs": [{"a": 767.4, "b": 928.1, "names": [], "ours": False}, {"a": 903.2, "b": 810.2,
                                                                              "names": ["Port Chester · Fire"], "ours": True}]}]
    h, d = tmp_path / "h.js", tmp_path / "in.json"
    h.write_text(DOM)
    d.write_text(json.dumps({"pages": [[rb_unknown, pc], [rb, pc], [ems]], "live": live}))
    r = subprocess.run([NODE, str(h), str(STATIC / "radio.js"), str(d)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    shot, both, ems_tags = out["tags"]
    assert shot[0]["parts"] == ["Unknown", "767.4/928.1"] and "unk" in shot[0]["cls"].split() and "ours" not in shot[0]["cls"]
    assert shot[1]["parts"] == ["Port Chester · Fire", "903.2/810.2"] and "ours" in shot[1]["cls"].split()
    assert shot[1]["title"] == "Port Chester · Fire: 903.2/810.2 Hz"
    assert [t["parts"] for t in both] == [["Rye Brook · Fire", "767.4/928.1"], ["Port Chester · Fire", "903.2/810.2"]]
    assert all("ours" in t["cls"].split() for t in both)
    assert ems_tags[0]["parts"] == ["Port Chester +2 · EMS", "928.1/510.5"] and "ours" not in ems_tags[0]["cls"].split()
    # the live row's words: pairs joined with " + " (" · " is inside the names), an unknown pair said so
    assert out["live"] == ["South 2 · Paging · unknown 767.4/928.1 + Port Chester · Fire"]
